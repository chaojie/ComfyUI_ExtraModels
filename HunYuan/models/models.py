import torch
import torch.nn as nn
import torch.nn.functional as F
from timm.models.vision_transformer import Mlp

from .attn_layers import Attention, FlashCrossMHAModified, FlashSelfMHAModified, CrossAttention
from .embedders import TimestepEmbedder, PatchEmbed, timestep_embedding
from .norm_layers import RMSNorm
from .poolers import AttentionPool
import folder_paths


class Resolution:
    def __init__(self, width, height):
        self.width = width
        self.height = height

    def __str__(self):
        return f'{self.height}x{self.width}'


class ResolutionGroup:
    def __init__(self):
        self.data = [
            Resolution(768, 768),   # 1:1
            Resolution(1024, 1024), # 1:1
            Resolution(1280, 1280), # 1:1
            Resolution(1024, 768),  # 4:3
            Resolution(1152, 864),  # 4:3
            Resolution(1280, 960),  # 4:3
            Resolution(768, 1024),  # 3:4
            Resolution(864, 1152),  # 3:4
            Resolution(960, 1280),  # 3:4
            Resolution(1280, 768),  # 16:9
            Resolution(768, 1280),  # 9:16
        ]
        self.supported_sizes = set([(r.width, r.height) for r in self.data])

    def is_valid(self, width, height):
        return (width, height) in self.supported_sizes

def modulate(x, shift, scale):
    return x * (1 + scale.unsqueeze(1)) + shift.unsqueeze(1)


class FP32_Layernorm(nn.LayerNorm):
    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        origin_dtype = inputs.dtype
        return F.layer_norm(inputs.float(), self.normalized_shape, self.weight.float(), self.bias.float(),
                            self.eps).to(origin_dtype)


class FP32_SiLU(nn.SiLU):
    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        return torch.nn.functional.silu(inputs.float(), inplace=False).to(inputs.dtype)


class HunYuanDiTBlock(nn.Module):
    """
    A HunYuanDiT block with `add` conditioning.
    """
    def __init__(self,
                 hidden_size,
                 c_emb_size,
                 num_heads,
                 mlp_ratio=4.0,
                 text_states_dim=1024,
                 use_flash_attn=False,
                 qk_norm=False,
                 norm_type="layer",
                 skip=False,
                 ):
        super().__init__()
        self.use_flash_attn = use_flash_attn
        use_ele_affine = True

        if norm_type == "layer":
            norm_layer = FP32_Layernorm
        elif norm_type == "rms":
            norm_layer = RMSNorm
        else:
            raise ValueError(f"Unknown norm_type: {norm_type}")

        # ========================= Self-Attention =========================
        self.norm1 = norm_layer(hidden_size, elementwise_affine=use_ele_affine, eps=1e-6)
        if use_flash_attn:
            self.attn1 = FlashSelfMHAModified(hidden_size, num_heads=num_heads, qkv_bias=True, qk_norm=qk_norm)
        else:
            self.attn1 = Attention(hidden_size, num_heads=num_heads, qkv_bias=True, qk_norm=qk_norm)

        # ========================= FFN =========================
        self.norm2 = norm_layer(hidden_size, elementwise_affine=use_ele_affine, eps=1e-6)
        mlp_hidden_dim = int(hidden_size * mlp_ratio)
        approx_gelu = lambda: nn.GELU(approximate="tanh")
        self.mlp = Mlp(in_features=hidden_size, hidden_features=mlp_hidden_dim, act_layer=approx_gelu, drop=0)

        # ========================= Add =========================
        # Simply use add like SDXL.
        self.default_modulation = nn.Sequential(
            FP32_SiLU(),
            nn.Linear(c_emb_size, hidden_size, bias=True)
        )

        # ========================= Cross-Attention =========================
        if use_flash_attn:
            self.attn2 = FlashCrossMHAModified(hidden_size, text_states_dim, num_heads=num_heads, qkv_bias=True,
                                               qk_norm=qk_norm)
        else:
            self.attn2 = CrossAttention(hidden_size, text_states_dim, num_heads=num_heads, qkv_bias=True,
                                        qk_norm=qk_norm)
        self.norm3 = norm_layer(hidden_size, elementwise_affine=True, eps=1e-6)

        # ========================= Skip Connection =========================
        if skip:
            self.skip_norm = norm_layer(2 * hidden_size, elementwise_affine=True, eps=1e-6)
            self.skip_linear = nn.Linear(2 * hidden_size, hidden_size)
        else:
            self.skip_linear = None

    def forward(self, x, c=None, text_states=None, freq_cis_img=None, skip=None):
        with open(f'{folder_paths.output_directory}/x1.txt', 'w') as file:
            file.write(f'x{x.shape}{x}')
        # Long Skip Connection
        if self.skip_linear is not None:
            cat = torch.cat([x, skip], dim=-1)
            cat = self.skip_norm(cat)
            x = self.skip_linear(cat)

        # Self-Attention
        shift_msa = self.default_modulation(c).unsqueeze(dim=1)
        with open(f'{folder_paths.output_directory}/shift_msa.txt', 'w') as file:
            file.write(f'shift_msa{shift_msa.shape}{shift_msa}')
        with open(f'{folder_paths.output_directory}/x.txt', 'w') as file:
            file.write(f'self.norm1(x){self.norm1(x).shape}{self.norm1(x)}')
        attn_inputs = (
            self.norm1(x) + shift_msa, freq_cis_img,
        )
        x = x + self.attn1(*attn_inputs)[0]

        # Cross-Attention
        cross_inputs = (
            self.norm3(x), text_states, freq_cis_img
        )
        x = x + self.attn2(*cross_inputs)[0]

        # FFN Layer
        mlp_inputs = self.norm2(x)
        x = x + self.mlp(mlp_inputs)

        return x


class FinalLayer(nn.Module):
    """
    The final layer of HunYuanDiT.
    """
    def __init__(self, final_hidden_size, c_emb_size, patch_size, out_channels):
        super().__init__()
        self.norm_final = nn.LayerNorm(final_hidden_size, elementwise_affine=False, eps=1e-6)
        self.linear = nn.Linear(final_hidden_size, patch_size * patch_size * out_channels, bias=True)
        self.adaLN_modulation = nn.Sequential(
            FP32_SiLU(),
            nn.Linear(c_emb_size, 2 * final_hidden_size, bias=True)
        )

    def forward(self, x, c):
        shift, scale = self.adaLN_modulation(c).chunk(2, dim=1)
        x = modulate(self.norm_final(x), shift, scale)
        x = self.linear(x)
        return x

class HunYuan(nn.Module):
    def __init__(
            self,
            input_size=(32, 32),
            patch_size=2,
            in_channels=4,
            hidden_size=1152,
            depth=28,
            num_heads=16,
            mlp_ratio=4.0,
            learn_sigma=True,
            text_states_dim=1024,
            text_states_dim_t5=2048,
            text_len=77,
            text_len_t5=256,
            norm="layer",
            infer_mode="torch",
            use_fp16=True,
            device="cuda",
            **kwargs,
    ):
        super().__init__()
        with open(f'{folder_paths.output_directory}/input_size.txt', 'w') as file:
            file.write(f'input_size{input_size}')
        self.device = device
        self.use_fp16=use_fp16
        self.dtype = torch.float16
        self.depth = depth
        self.learn_sigma = learn_sigma
        self.in_channels = in_channels
        self.out_channels = in_channels * 2 if learn_sigma else in_channels
        self.patch_size = patch_size
        self.num_heads = num_heads
        self.hidden_size = hidden_size
        self.text_states_dim = text_states_dim
        self.text_states_dim_t5 = text_states_dim_t5
        self.text_len = text_len
        self.text_len_t5 = text_len_t5
        self.norm = norm
        self.head_size = self.hidden_size // self.num_heads

        use_flash_attn = infer_mode == 'fa'
        qk_norm = True  # See http://arxiv.org/abs/2302.05442 for details.

        self.mlp_t5 = nn.Sequential(
            nn.Linear(self.text_states_dim_t5, self.text_states_dim_t5 * 4, bias=True),
            FP32_SiLU(),
            nn.Linear(self.text_states_dim_t5 * 4, self.text_states_dim, bias=True),
        )
        # learnable replace
        self.text_embedding_padding = nn.Parameter(
            torch.randn(self.text_len + self.text_len_t5, self.text_states_dim, dtype=torch.float32))

        # Attention pooling
        self.pooler = AttentionPool(self.text_len_t5, self.text_states_dim_t5, num_heads=8, output_dim=1024)

        # Here we use a default learned embedder layer for future extension.
        self.style_embedder = nn.Embedding(1, hidden_size)

        # Image size and crop size conditions
        self.extra_in_dim = 256 * 6 + hidden_size

        # Text embedding for `add`
        self.x_embedder = PatchEmbed(input_size, patch_size, in_channels, hidden_size)
        self.t_embedder = TimestepEmbedder(hidden_size)
        self.extra_in_dim += 1024
        self.extra_embedder = nn.Sequential(
            nn.Linear(self.extra_in_dim, hidden_size * 4),
            FP32_SiLU(),
            nn.Linear(hidden_size * 4, hidden_size, bias=True),
        )

        # Image embedding
        num_patches = self.x_embedder.num_patches

        # HUnYuanDiT Blocks
        self.blocks = nn.ModuleList([
            HunYuanDiTBlock(hidden_size=hidden_size,
                            c_emb_size=hidden_size,
                            num_heads=num_heads,
                            mlp_ratio=mlp_ratio,
                            text_states_dim=self.text_states_dim,
                            use_flash_attn=use_flash_attn,
                            qk_norm=qk_norm,
                            norm_type=self.norm,
                            skip=layer > depth // 2,
                            )
            for layer in range(depth)
        ])

        self.final_layer = FinalLayer(hidden_size, hidden_size, patch_size, self.out_channels)
        self.unpatchify_channels = self.out_channels

        self.initialize_weights()


    def extra_conds(self, **kwargs):
        out = {}
        print(f'extra_conds_kwargs{kwargs}')
        with open(f'{folder_paths.output_directory}/extra_conds_kwargs.txt', 'w') as file:
            file.write(f'extra_conds_kwargs{kwargs}')

        return out

    def calc_rope(self, height, width):
        from .posemb_layers import get_2d_rotary_pos_embed, get_fill_resize_and_crop
        th = height // 8 // self.patch_size
        tw = width // 8 // self.patch_size
        base_size = 512 // 8 // self.patch_size
        start, stop = get_fill_resize_and_crop((th, tw), base_size)
        sub_args = [start, stop, (th, tw)]
        rope = get_2d_rotary_pos_embed(self.head_size, *sub_args)
        return rope
    
    def standard_shapes(self):
        resolutions = ResolutionGroup()
        freqs_cis_img = {}
        for reso in resolutions.data:
            freqs_cis_img[str(reso)] = self.calc_rope(reso.height, reso.width)
        return resolutions, freqs_cis_img

    def forward(self, x, timesteps, context, y=None, **kwargs):
        with open(f'{folder_paths.output_directory}/x.txt', 'w') as file:
            file.write(f'x{x.shape}')
        with open(f'{folder_paths.output_directory}/context.txt', 'w') as file:
            file.write(f'context{context}')
        with open(f'{folder_paths.output_directory}/y.txt', 'w') as file:
            file.write(f'y{y}')
        with open(f'{folder_paths.output_directory}/kwargs.txt', 'w') as file:
            file.write(f'kwargs{kwargs}')
        #with torch.cuda.amp.autocast():
        context = context[:, 0]

        ## run original forward pass
        out = self.forward_raw(
            x = x.to(self.dtype),
            t = timesteps.to(self.dtype),
            y = context.to(torch.int),
        )

        ## only return EPS
        out = out.to(torch.float16)
        #torch.save(out,f"{folder_paths.output_directory}/out.pt")
        eps, rest = out[:, :self.in_channels], out[:, self.in_channels:]
        #torch.save(eps,f"{folder_paths.output_directory}/eps.pt")
        return eps[:x.shape[0]]
        

    def forward_raw(self,
                x,
                t,
                y,
                encoder_hidden_states=None,
                text_embedding_mask=None,
                encoder_hidden_states_t5=None,
                text_embedding_mask_t5=None,
                image_meta_size=None,
                style=None,
                cos_cis_img=None,
                sin_cis_img=None,
                return_dict=False,
                ):
        """
        Forward pass of the encoder.

        Parameters
        ----------
        x: torch.Tensor
            (B, D, H, W)
        t: torch.Tensor
            (B)
        y: (N, 1, 120, C) tensor of class labels
        encoder_hidden_states: torch.Tensor
            CLIP text embedding, (B, L_clip, D)
        text_embedding_mask: torch.Tensor
            CLIP text embedding mask, (B, L_clip)
        encoder_hidden_states_t5: torch.Tensor
            T5 text embedding, (B, L_t5, D)
        text_embedding_mask_t5: torch.Tensor
            T5 text embedding mask, (B, L_t5)
        image_meta_size: torch.Tensor
            (B, 6)
        style: torch.Tensor
            (B)
        cos_cis_img: torch.Tensor
        sin_cis_img: torch.Tensor
        return_dict: bool
            Whether to return a dictionary.
        """
        
        ob, _, oh, ow = x.shape
        batch_size=ob
        clip_prompt_embeds=torch.load(f"{folder_paths.output_directory}/clip_prompt_embeds.pt").half().repeat(batch_size,1,1)    
        clip_attention_mask=torch.load(f"{folder_paths.output_directory}/clip_attention_mask.pt").half().repeat(batch_size,1)    
        clip_negative_prompt_embeds=torch.load(f"{folder_paths.output_directory}/clip_negative_prompt_embeds.pt").half().repeat(batch_size,1,1)    
        clip_negative_attention_mask=torch.load(f"{folder_paths.output_directory}/clip_negative_attention_mask.pt").half().repeat(batch_size,1)    
        mt5_prompt_embeds=torch.load(f"{folder_paths.output_directory}/mt5_prompt_embeds.pt").half().repeat(batch_size,1,1)    
        mt5_attention_mask=torch.load(f"{folder_paths.output_directory}/mt5_attention_mask.pt").half().repeat(batch_size,1)    
        mt5_negative_prompt_embeds=torch.load(f"{folder_paths.output_directory}/mt5_negative_prompt_embeds.pt").half().repeat(batch_size,1,1)    
        mt5_negative_attention_mask=torch.load(f"{folder_paths.output_directory}/mt5_negative_attention_mask.pt").half().repeat(batch_size,1)    

        encoder_hidden_states=torch.cat((clip_prompt_embeds,clip_negative_prompt_embeds))
        encoder_hidden_states_t5=torch.cat((mt5_prompt_embeds,mt5_negative_prompt_embeds))   
        text_embedding_mask=torch.cat((clip_attention_mask,clip_negative_attention_mask))   
        text_embedding_mask_t5=torch.cat((mt5_attention_mask,mt5_negative_attention_mask))  


        text_states = encoder_hidden_states                     # 2,77,1024
        text_states_t5 = encoder_hidden_states_t5               # 2,256,2048
        text_states_mask = text_embedding_mask.bool()           # 2,77
        text_states_t5_mask = text_embedding_mask_t5.bool()     # 2,256
        b_t5, l_t5, c_t5 = text_states_t5.shape
        text_states_t5 = self.mlp_t5(text_states_t5.view(-1, c_t5))
        text_states = torch.cat([text_states, text_states_t5.view(b_t5, l_t5, -1)], dim=1)  # 2,205，1024
        clip_t5_mask = torch.cat([text_states_mask, text_states_t5_mask], dim=-1)

        clip_t5_mask = clip_t5_mask
        text_states = torch.where(clip_t5_mask.unsqueeze(2), text_states, self.text_embedding_padding.to(text_states))

        th, tw = oh // self.patch_size, ow // self.patch_size

        # ========================= Build time and image embedding =========================
        t=t.repeat(2)
        x=x.repeat(2,1,1,1)
        t = self.t_embedder(t)
        x = self.x_embedder(x)
        with open(f'{folder_paths.output_directory}/x3.txt', 'w') as file:
            file.write(f'x{x.shape}{x}')
        #y = y.to(self.dtype)

        # Get image RoPE embedding according to `reso`lution.
        freqs_cis_img = (cos_cis_img, sin_cis_img)

        # ========================= Concatenate all extra vectors =========================
        # Build text tokens with pooling

        extra_vec = self.pooler(encoder_hidden_states_t5)

        height=oh*8
        width=ow*8
        target_height = int((height // 16) * 16)
        target_width = int((width // 16) * 16)

        # Build image meta size tokens
        size_cond = list((1024,1024)) + [target_width, target_height, 0, 0]
        image_meta_size = torch.as_tensor([size_cond] * 2 * batch_size, device=self.device)

        with open(f'{folder_paths.output_directory}/image_meta_size.txt', 'w') as file:
            file.write(f'image_meta_size{image_meta_size.shape}{image_meta_size}')

        image_meta_size = timestep_embedding(image_meta_size.view(-1), 256)   # [B * 6, 256]
        if self.use_fp16:
            image_meta_size = image_meta_size.half()
        image_meta_size = image_meta_size.view(-1, 6 * 256)
        
        extra_vec = torch.cat([extra_vec, image_meta_size], dim=1)  # [B, D + 6 * 256]

        # Build style tokens
        style = torch.as_tensor([0, 0] * batch_size, device=self.device)

        style_embedding = self.style_embedder(style)
        extra_vec = torch.cat([extra_vec, style_embedding], dim=1)

        # Concatenate all extra vectors
        resolutions, freqs_cis_img = self.standard_shapes() 
        
        reso = f'{target_height}x{target_width}'
        if reso in freqs_cis_img:
            freqs_cis_img = freqs_cis_img[reso]
        else:
            freqs_cis_img = self.calc_rope(target_height, target_width)

        with open(f'{folder_paths.output_directory}/t.txt', 'w') as file:
            file.write(f't{t.shape}{t}')
        with open(f'{folder_paths.output_directory}/extra_vec.txt', 'w') as file:
            file.write(f'extra_vec{extra_vec.shape}{extra_vec}')
        c = t + self.extra_embedder(extra_vec)  # [B, D]

        # ========================= Forward pass through HunYuanDiT blocks =========================
        skips = []
        for layer, block in enumerate(self.blocks):
            if layer > self.depth // 2:
                skip = skips.pop()
                x = block(x, c, text_states, freqs_cis_img, skip)   # (N, L, D)
            else:
                x = block(x, c, text_states, freqs_cis_img)         # (N, L, D)

            if layer < (self.depth // 2 - 1):
                skips.append(x)

        # ========================= Final layer =========================
        x = self.final_layer(x, c)                              # (N, L, patch_size ** 2 * out_channels)
        x = self.unpatchify(x, th, tw)                          # (N, out_channels, H, W)

        if return_dict:
            return {'x': x}
        return x

    def initialize_weights(self):
        # Initialize transformer layers:
        def _basic_init(module):
            if isinstance(module, nn.Linear):
                torch.nn.init.xavier_uniform_(module.weight)
                if module.bias is not None:
                    nn.init.constant_(module.bias, 0)
        self.apply(_basic_init)

        # Initialize patch_embed like nn.Linear (instead of nn.Conv2d):
        w = self.x_embedder.proj.weight.data
        nn.init.xavier_uniform_(w.view([w.shape[0], -1]))
        nn.init.constant_(self.x_embedder.proj.bias, 0)

        # Initialize label embedding table:
        nn.init.normal_(self.extra_embedder[0].weight, std=0.02)
        nn.init.normal_(self.extra_embedder[2].weight, std=0.02)

        # Initialize timestep embedding MLP:
        nn.init.normal_(self.t_embedder.mlp[0].weight, std=0.02)
        nn.init.normal_(self.t_embedder.mlp[2].weight, std=0.02)

        # Zero-out adaLN modulation layers in HunYuanDiT blocks:
        for block in self.blocks:
            nn.init.constant_(block.default_modulation[-1].weight, 0)
            nn.init.constant_(block.default_modulation[-1].bias, 0)

        # Zero-out output layers:
        nn.init.constant_(self.final_layer.adaLN_modulation[-1].weight, 0)
        nn.init.constant_(self.final_layer.adaLN_modulation[-1].bias, 0)
        nn.init.constant_(self.final_layer.linear.weight, 0)
        nn.init.constant_(self.final_layer.linear.bias, 0)

    def unpatchify(self, x, h, w):
        """
        x: (N, T, patch_size**2 * C)
        imgs: (N, H, W, C)
        """
        c = self.unpatchify_channels
        p = self.x_embedder.patch_size[0]
        # h = w = int(x.shape[1] ** 0.5)
        assert h * w == x.shape[1]

        x = x.reshape(shape=(x.shape[0], h, w, p, p, c))
        x = torch.einsum('nhwpqc->nchpwq', x)
        imgs = x.reshape(shape=(x.shape[0], c, h * p, w * p))
        return imgs

#################################################################################
#                            HunYuanDiT Configs                                 #
#################################################################################

HUNYUAN_DIT_CONFIG = {
    'DiT-g/2': {'depth': 40, 'hidden_size': 1408, 'patch_size': 2, 'num_heads': 16, 'mlp_ratio': 4.3637},
    'DiT-XL/2': {'depth': 28, 'hidden_size': 1152, 'patch_size': 2, 'num_heads': 16},
    'DiT-L/2': {'depth': 24, 'hidden_size': 1024, 'patch_size': 2, 'num_heads': 16},
    'DiT-B/2': {'depth': 12, 'hidden_size': 768, 'patch_size': 2, 'num_heads': 12},
}
