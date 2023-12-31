import torch.nn as nn
import models.norms as norms
import torch
import torch.nn.functional as F
# from .blocks import *
from . import blocks as CIPblocks
from models.discriminator import make_kernel,upfirdn2d,InverseHaarTransform,HaarTransform,ModulatedConv2d

import models.tensor_transforms as tt
















class OASIS_Generator(nn.Module):
    def __init__(self, opt):
        super().__init__()
        self.opt = opt
        sp_norm = norms.get_spectral_norm(opt)
        ch = opt.channels_G
        self.channels = [16*ch, 16*ch, 16*ch, 8*ch, 4*ch, 2*ch, 1*ch]
        self.init_W, self.init_H = self.compute_latent_vector_size(opt)
        self.conv_img = nn.Conv2d(self.channels[-1], 3, 3, padding=1)
        self.add_edges = 1 if opt.add_edges else 0
        #self.conv_img =
        # (self.channels[-1])
        self.up = nn.Upsample(scale_factor=2)
        self.body = nn.ModuleList([])
        if self.opt.progressive_growing :
            self.torgbs = nn.ModuleList([])
        for i in range(len(self.channels)-1):
            self.body.append(ResnetBlock_with_SPADE(self.channels[i], self.channels[i+1], opt))
            if self.opt.progressive_growing:
                self.torgbs.append(ToRGB(in_channel=self.channels[i+1]))
        if not self.opt.no_3dnoise:
            self.fc = nn.Conv2d(self.opt.semantic_nc + self.opt.z_dim+self.add_edges, 16 * ch, 3, padding=1)
        else:
            self.fc = nn.Conv2d(self.opt.semantic_nc+self.add_edges, 16 * ch, 3, padding=1)

    def compute_latent_vector_size(self, opt):
        w = opt.crop_size // (2**(opt.num_res_blocks-1))
        h = round(w / opt.aspect_ratio)
        return h, w

    def forward(self, input, z=None,edges = None):
        seg = input
        if self.opt.gpu_ids != "-1":
            seg.cuda()
        if not self.opt.no_3dnoise:
            dev = seg.get_device() if self.opt.gpu_ids != "-1" else "cpu"
            z = torch.randn(seg.size(0), self.opt.z_dim, dtype=torch.float32, device=dev)
            z = z.view(z.size(0), self.opt.z_dim, 1, 1)
            z = z.expand(z.size(0), self.opt.z_dim, seg.size(2), seg.size(3))
            seg = torch.cat((z, seg), dim = 1)
        if self.add_edges :
            x = F.interpolate(torch.cat((seg,edges),dim = 1), size=(self.init_W, self.init_H))
        else :
            x = F.interpolate(seg, size=(self.init_W, self.init_H))
        x = self.fc(x)
        out = None
        for i in range(self.opt.num_res_blocks):
            x = self.body[i](x, seg,edges)

            if self.opt.progressive_growing and out == None :
                out = self.torgbs[i](x)
            elif self.opt.progressive_growing :
                out = self.torgbs[i](x,skip = out)

            if i < self.opt.num_res_blocks-1:
                x = self.up(x)

        if self.opt.progressive_growing :
            x = out
            x = F.tanh(x)
        else :
            x = self.conv_img(F.leaky_relu(x, 2e-1))
            x = F.tanh(x)
            print("forward used","output=",x.shape)

        return x

    def forward_determinstic(self, input, noise_vector):
        seg = input
        edges = None
        if self.opt.gpu_ids != "-1":
            seg.cuda()
        if not self.opt.no_3dnoise:
            dev = seg.get_device() if self.opt.gpu_ids != "-1" else "cpu"
            z = noise_vector.to(dev)
            seg = torch.cat((z, seg), dim = 1)
        if self.add_edges :
            x = F.interpolate(torch.cat((seg,edges),dim = 1), size=(self.init_W, self.init_H))
        else :
            x = F.interpolate(seg, size=(self.init_W, self.init_H))
        x = self.fc(x)
        out = None
        for i in range(self.opt.num_res_blocks):
            x = self.body[i](x, seg,edges)

            if self.opt.progressive_growing and out == None :
                out = self.torgbs[i](x)
            elif self.opt.progressive_growing :
                out = self.torgbs[i](x,skip = out)

            if i < self.opt.num_res_blocks-1:
                x = self.up(x)

        if self.opt.progressive_growing :
            x = out
            x = F.tanh(x)
        else :
            x = self.conv_img(F.leaky_relu(x, 2e-1))
            x = F.tanh(x)
            print("forward_determinstic used", "output=", x.shape)
        return x











class ImplicitGenerator(nn.Module):




    def __init__(self, opt=None,size=(256,512), hidden_size=512, n_mlp=8, style_dim=512, lr_mlp=0.01,
                 activation=None, channel_multiplier=2,z=None, **kwargs):
        super(ImplicitGenerator, self).__init__()

        self.opt = opt
        if opt.apply_MOD_CLADE:
            self.approach = 0
        elif opt.only_CLADE:
            self.approach = 1
        elif opt.Matrix_Computation:
            self.approach = 2
        else:
            self.approach = -1

        self.tanh = nn.Tanh()

        self.size = size
        demodulate = True
        self.demodulate = demodulate
        self.lff = CIPblocks.LFF(hidden_size)
        self.emb = CIPblocks.ConstantInput(hidden_size, size=size)

        self.channels = {
            0: 512,
            1: 512,##512
            2: 512,##512
            3: 512,##512
            4: 256 * channel_multiplier,
            5: 128 * channel_multiplier,
            6: 64 * channel_multiplier,
            7: 32 * channel_multiplier,
            8: 16 * channel_multiplier,
        }

        multiplier = 2
        in_channels = int(self.channels[0])
        self.conv1 = CIPblocks.StyledConv(int(multiplier*hidden_size),##the real in_channel 1024
                                          in_channels,##actually is out_channel
                                          1,
                                          style_dim,
                                          demodulate=demodulate,
                                          activation=activation,
                                          approach=self.approach,#jhl
                                          )
        ###kernel_size = 1===>first modFC layer!!only one layer!!input=embbed coords!!

        self.linears = nn.ModuleList()
        ##2xModFC for 2-8 Layers
        self.to_rgbs = nn.ModuleList()
        ##tRGB for 2-8 Layers
        self.log_size = int(CIPblocks.math.log(max(size), 2))
        ## 8 Layers

        self.n_intermediate = self.log_size - 1
        ## intermediate layer(7 layers except first layer)
        self.to_rgb_stride = 2
        ##how many ModFC between two tRGB==>in this case, 2 ModFC layers
        for i in range(0, self.log_size - 1):## for each layer in intermediate 7 Layers:
            out_channels = self.channels[i]
            self.linears.append(CIPblocks.StyledConv(in_channels, out_channels, 1, style_dim,
                                           demodulate=demodulate, activation=activation,approach=self.approach,))#jhl
            self.linears.append(CIPblocks.StyledConv(out_channels, out_channels, 1, style_dim,
                                           demodulate=demodulate, activation=activation,approach=self.approach,))#jhl
            self.to_rgbs.append(CIPblocks.ToRGB(out_channels, style_dim, upsample=False,approach=self.approach,))#jhl
                                                                                        ###upsample turned off manually
            # print(out_channels)
            in_channels = out_channels
            ##2xModFC+tRGB for 2-8 Layers

        self.style_dim = style_dim
        ##dimension of style vector

        layers = [CIPblocks.PixelNorm()]
        ##layers for latent normalization

        for i in range(n_mlp):##mapping network for style w(in total 8 layers)
            layers.append(
                CIPblocks.EqualLinear(
                    style_dim, style_dim, lr_mul=lr_mlp, activation='fused_lrelu'
                )
            )

        self.style = nn.Sequential(*layers)
        ##mapping network that generate style w!!




        self.styleMatrix = nn.Parameter(torch.randn(35,512))
        # self.styleMatrix.data.fill_(0.25)
        # self.alpha = nn.Parameter(torch.rand(1,512))
        # self.alpha.data.fill_(0.5)




    def forward(self,
                label,##[1,35,256,512]
                label_class_dict,
                coords,##[1,2,256,512]
                latent,##1D list[Tensor(1,512)]
                return_latents=False,
                truncation=1,
                truncation_latent=None,
                input_is_latent=False,
                edges=None,
                ):
        # print("input latent code:",latent)
        latent = latent[0]##[1,512]
        ##input noirse z
        # print("received latent[0] :",latent.shape,latent)
        if truncation < 1:
            latent = truncation_latent + truncation * (latent - truncation_latent)

        if not input_is_latent:
            latent = self.style(latent)
        ##style w [1,512]


        # latent = self.alpha*latent + (1-self.alpha)*self.styleMatrix
        ##combined style vector [35,512]


        x = self.lff(coords)
        ##Fourier Features:simple linear transformation with sin activation
            ##[N,512,256,512]
        # print(x)

        batch_size, _, h, w = coords.shape
        if self.training and h == self.size[0] and w == self.size[1]:
            emb = self.emb(x)
        else:
            emb = F.grid_sample(
                #Given an input and a flow-field grid,
                #computes the output using input values and pixel locations from grid.
                #input(N,C,H_in,W_in),grid(N,2,H_out,W_out),out(N,C,H_out,W_out)
                self.emb.learnable_vectors.expand(batch_size, -1, -1, -1),
                #调用emb class的self.input!!
                # -1 means not changing the size of that dimension!!!!
                coords.permute(0, 2, 3, 1).contiguous(),
                padding_mode='border', mode='bilinear',
            )
        ##generate coordinate embedding for 256x512
        ##[1,512,256,512]

        x = torch.cat([x, emb], 1)
        ##concatenation of Fourier Features and Coordinates Embeddings on channel dimension!!!
        ##[1,1024,256,512]

        rgb = 0

        x = self.conv1(x, latent,label_class_dict=label_class_dict,label=label,class_style=self.styleMatrix,)
        ##first ModFC layer
        for i in range(self.n_intermediate):##2-8 ModFC layers
            # print(i)
            for j in range(self.to_rgb_stride):##2xModFC
                x = self.linears[i*self.to_rgb_stride + j](x, latent,label_class_dict=label_class_dict,label=label,class_style=self.styleMatrix,)

            rgb = self.to_rgbs[i](x, latent, rgb,label_class_dict=label_class_dict,label=label,class_style=self.styleMatrix,)
                                        ####skip=rgb ==> rgb image accumulation!!

        if return_latents:
            return rgb, latent
        else:

            # print("rgb size:",rgb.size())
            # return self.tanh(rgb), None
            return rgb, None








class ImplicitGenerator_tanh(nn.Module):

    def __init__(self, opt=None, size=(256, 512), hidden_size=512, n_mlp=8, style_dim=512, lr_mlp=0.01,
                 activation=None, channel_multiplier=2, z=None, **kwargs):
        super(ImplicitGenerator_tanh, self).__init__()

        self.opt = opt
        if opt.apply_MOD_CLADE:
            self.approach = 0
        elif opt.only_CLADE:
            self.approach = 1
        elif opt.Matrix_Computation:
            self.approach = 2
        else:
            self.approach = -1

        self.tanh = nn.Tanh()

        self.size = size
        demodulate = True
        self.demodulate = demodulate
        self.lff = CIPblocks.LFF(hidden_size)
        self.emb = CIPblocks.ConstantInput(hidden_size, size=size)

        self.channels = {
            0: 512,
            1: 512,  ##512
            2: 512,  ##512
            3: 512,  ##512
            4: 256 * channel_multiplier,
            5: 128 * channel_multiplier,
            6: 64 * channel_multiplier,
            7: 32 * channel_multiplier,
            8: 16 * channel_multiplier,
        }

        multiplier = 2
        in_channels = int(self.channels[0])
        self.conv1 = CIPblocks.StyledConv(int(multiplier * hidden_size),  ##the real in_channel 1024
                                          in_channels,  ##actually is out_channel
                                          1,
                                          style_dim,
                                          demodulate=demodulate,
                                          activation=activation,
                                          approach=self.approach,  # jhl
                                          )
        ###kernel_size = 1===>first modFC layer!!only one layer!!input=embbed coords!!

        self.linears = nn.ModuleList()
        ##2xModFC for 2-8 Layers
        self.to_rgbs = nn.ModuleList()
        ##tRGB for 2-8 Layers
        self.log_size = int(CIPblocks.math.log(max(size), 2))
        ## 8 Layers

        self.n_intermediate = self.log_size - 1
        ## intermediate layer(7 layers except first layer)
        self.to_rgb_stride = 2
        ##how many ModFC between two tRGB==>in this case, 2 ModFC layers
        for i in range(0, self.log_size - 1):  ## for each layer in intermediate 7 Layers:
            out_channels = self.channels[i]
            self.linears.append(CIPblocks.StyledConv(in_channels, out_channels, 1, style_dim,
                                                     demodulate=demodulate, activation=activation,
                                                     approach=self.approach, ))  # jhl
            self.linears.append(CIPblocks.StyledConv(out_channels, out_channels, 1, style_dim,
                                                     demodulate=demodulate, activation=activation,
                                                     approach=self.approach, ))  # jhl
            self.to_rgbs.append(
                CIPblocks.ToRGB(out_channels, style_dim, upsample=False, approach=self.approach, ))  # jhl
            ###upsample turned off manually
            # print(out_channels)
            in_channels = out_channels
            ##2xModFC+tRGB for 2-8 Layers

        self.style_dim = style_dim
        ##dimension of style vector

        layers = [CIPblocks.PixelNorm()]
        ##layers for latent normalization

        for i in range(n_mlp):  ##mapping network for style w(in total 8 layers)
            layers.append(
                CIPblocks.EqualLinear(
                    style_dim, style_dim, lr_mul=lr_mlp, activation='fused_lrelu'
                )
            )

        self.style = nn.Sequential(*layers)
        ##mapping network that generate style w!!

        self.styleMatrix = nn.Parameter(torch.randn(35, 512))
        # self.styleMatrix.data.fill_(0.25)
        # self.alpha = nn.Parameter(torch.rand(1,512))
        # self.alpha.data.fill_(0.5)

    def forward(self,
                label,  ##[1,35,256,512]
                label_class_dict,
                coords,  ##[1,2,256,512]
                latent,  ##1D list[Tensor(1,512)]
                return_latents=False,
                truncation=1,
                truncation_latent=None,
                input_is_latent=False,
                edges=None,
                ):
        # print("input latent code:",latent)
        latent = latent[0]  ##[1,512]
        ##input noirse z
        # print("received latent[0] :",latent.shape,latent)
        if truncation < 1:
            latent = truncation_latent + truncation * (latent - truncation_latent)

        if not input_is_latent:
            latent = self.style(latent)
        ##style w [1,512]

        # latent = self.alpha*latent + (1-self.alpha)*self.styleMatrix
        ##combined style vector [35,512]

        x = self.lff(coords)
        ##Fourier Features:simple linear transformation with sin activation
        ##[N,512,256,512]
        # print(x)

        batch_size, _, h, w = coords.shape
        if self.training and h == self.size[0] and w == self.size[1]:
            emb = self.emb(x)
        else:
            emb = F.grid_sample(
                # Given an input and a flow-field grid,
                # computes the output using input values and pixel locations from grid.
                # input(N,C,H_in,W_in),grid(N,2,H_out,W_out),out(N,C,H_out,W_out)
                self.emb.learnable_vectors.expand(batch_size, -1, -1, -1),
                # 调用emb class的self.input!!
                # -1 means not changing the size of that dimension!!!!
                coords.permute(0, 2, 3, 1).contiguous(),
                padding_mode='border', mode='bilinear',
            )
        ##generate coordinate embedding for 256x512
        ##[1,512,256,512]

        x = torch.cat([x, emb], 1)
        ##concatenation of Fourier Features and Coordinates Embeddings on channel dimension!!!
        ##[1,1024,256,512]

        rgb = 0

        x = self.conv1(x, latent, label_class_dict=label_class_dict, label=label, class_style=self.styleMatrix, )
        ##first ModFC layer
        for i in range(self.n_intermediate):  ##2-8 ModFC layers
            # print(i)
            for j in range(self.to_rgb_stride):  ##2xModFC
                x = self.linears[i * self.to_rgb_stride + j](x, latent, label_class_dict=label_class_dict, label=label,
                                                             class_style=self.styleMatrix, )

            rgb = self.to_rgbs[i](x, latent, rgb, label_class_dict=label_class_dict, label=label,
                                  class_style=self.styleMatrix, )
            ####skip=rgb ==> rgb image accumulation!!

        if return_latents:
            return rgb, latent
        else:

            # print("rgb size:",rgb.size())
            # return self.tanh(rgb), None
            return self.tanh(rgb), None









class ImplicitGenerator_2scale_(nn.Module):




    def __init__(self, opt=None,size=(128,256), hidden_size=512, n_mlp=8, style_dim=512, lr_mlp=0.01,
                 activation=None,
                 channel_multiplier=2,z=None, **kwargs):
        super(ImplicitGenerator_2scale_, self).__init__()

        self.opt = opt
        if opt.apply_MOD_CLADE:
            self.approach = 0
        elif opt.only_CLADE:
            self.approach = 1
        elif opt.Matrix_Computation:
            self.approach = 2
        else:
            self.approach = -1

        self.tanh = nn.Tanh()

        self.starting_size = size  ##starting size!!
        demodulate = True
        self.demodulate = demodulate
        self.lff = CIPblocks.LFF(hidden_size)
        self.lff1 = CIPblocks.LFF(int(hidden_size/2))

        # print(size)
        self.emb = CIPblocks.ConstantInput(hidden_size, size=size)

        self.channels = {
            0: 512,
            1: 512,##512
            2: 512,##512
            3: 512,##512
            4: 256 * channel_multiplier,
            5: 128 * channel_multiplier,
            6: 64 * channel_multiplier,
            7: 32 * channel_multiplier,
            8: 16 * channel_multiplier,
        }

        multiplier = 2

        self.coords0 = tt.convert_to_coord_format(opt.batch_size, self.starting_size[0], self.starting_size[1], integer_values=False)

        in_channels = int(self.channels[0])
        self.conv1 = CIPblocks.StyledConv(int(multiplier*hidden_size),##the real in_channel 1024
                                          in_channels,##actually is out_channel
                                          1,
                                          style_dim,
                                          demodulate=demodulate,
                                          activation=activation,
                                          approach=self.approach,#jhl
                                          )
        self.conv2 = CIPblocks.StyledConv(512,  ##the real in_channel 1024
                                          256,  ##actually is out_channel
                                          1,
                                          style_dim,
                                          demodulate=demodulate,
                                          activation=activation,
                                          approach=self.approach,  # jhl
                                          )
        ###kernel_size = 1===>first modFC layer!!only one layer!!input=embbed coords!!

        self.linears = nn.ModuleList()
        ##2xModFC for 2-8 Layers
        self.to_rgbs = nn.ModuleList()
        ##tRGB for 2-8 Layers

        self.log_size = int(CIPblocks.math.log(512, 2))
        ## 8 Layers

        self.n_intermediate = self.log_size - 1
        ## intermediate layer(7 layers except first layer)
        self.to_rgb_stride = 2
        ##how many ModFC between two tRGB==>in this case, 2 ModFC layers
        for i in range(0, self.log_size - 1):## for each layer in intermediate 7 Layers:

            out_channels = self.channels[i]
            self.linears.append(CIPblocks.StyledConv(in_channels, out_channels, 1, style_dim,
                                           demodulate=demodulate, activation=activation,approach=self.approach,))#jhl
            self.linears.append(CIPblocks.StyledConv(out_channels, out_channels, 1, style_dim,
                                           demodulate=demodulate, activation=activation,approach=self.approach,))#jhl

            self.to_rgbs.append(CIPblocks.ToRGB(out_channels, style_dim, upsample=False,approach=self.approach,))#jhl
                                                                                        ###upsample turned off manually
            # print(out_channels)
            in_channels = out_channels
            ##2xModFC+tRGB for 2-8 Layers



        self.style_dim = style_dim
        ##dimension of style vector

        layers = [CIPblocks.PixelNorm()]
        ##layers for latent normalization

        for i in range(n_mlp):##mapping network for style w(in total 8 layers)
            layers.append(
                CIPblocks.EqualLinear(
                    style_dim, style_dim, lr_mul=lr_mlp, activation='fused_lrelu'
                )
            )

        self.style = nn.Sequential(*layers)
        ##mapping network that generate style w!!




        self.styleMatrix = nn.Parameter(torch.randn(35,512))
        # self.styleMatrix.data.fill_(0.25)
        # self.alpha = nn.Parameter(torch.rand(1,512))
        # self.alpha.data.fill_(0.5)




    def forward(self,
                label,##[1,35,256,512]
                label_class_dict,
                coords,##[1,2,256,512]
                latent,##1D list[Tensor(1,512)]
                return_latents=False,
                truncation=1,
                truncation_latent=None,
                input_is_latent=False,
                edges=None,
                ):
        # print("input latent code:",latent)
        latent = latent[0]##[1,512]
        ##input noirse z
        # print("received latent[0] :",latent.shape,latent)
        if truncation < 1:
            latent = truncation_latent + truncation * (latent - truncation_latent)

        if not input_is_latent:
            latent = self.style(latent)
        ##style w [1,512]


        # latent = self.alpha*latent + (1-self.alpha)*self.styleMatrix
        ##combined style vector [35,512]


        x = self.lff(self.coords0)
        ##Fourier Features:simple linear transformation with sin activation
            ##[N,512,256,512]
        # print(x)

        batch_size, _, h, w = coords.shape

        emb = self.emb(x)

        # print(x.shape,emb.shape)
        x = torch.cat([x, emb], 1)

        ##concatenation of Fourier Features and Coordinates Embeddings on channel dimension!!!
        ##[1,1024,256,512]

        rgb = 0

        label_256_512 = label
        label_class_dict_256_512 = label_class_dict
        label_128_256 = F.interpolate(label,  ##!!  interpolate can also downscale!!!
                                      size=(128,256),  ##take the H and W
                                      mode='nearest')### nearest or bilinear???


        label_class_dict_128_256 = torch.argmax(label_128_256, 1).long()

        label = label_128_256
        label_class_dict = label_class_dict_128_256

        x = self.conv1(x, latent,label_class_dict=label_class_dict,label=label,class_style=self.styleMatrix,)
        ##first ModFC layer



        for i in range(self.n_intermediate):##2-8 ModFC layers

            if x.shape[1] == 256:
                label = label_256_512
                label_class_dict = label_class_dict_256_512
                x = F.interpolate(x,scale_factor=2,mode = 'nearest')
                x = torch.cat([x,self.lff1(coords)],1)
                x = self.conv2(x, latent, label_class_dict=label_class_dict, label=label,
                               class_style=self.styleMatrix, )
                rgb = F.interpolate(rgb,scale_factor=2,mode = 'nearest')


            for j in range(self.to_rgb_stride):##2xModFC
                x = self.linears[i*self.to_rgb_stride + j](x, latent,label_class_dict=label_class_dict,label=label,class_style=self.styleMatrix,)



            rgb = self.to_rgbs[i](x, latent, rgb,label_class_dict=label_class_dict,label=label,class_style=self.styleMatrix,)
                                        ####skip=rgb ==> rgb image accumulation!!

        if return_latents:
            return rgb, latent
        else:

            # print("rgb size:",rgb.size())
            # return self.tanh(rgb), None
            return self.tanh(rgb), None





class ImplicitGenerator_multiscale_(nn.Module):




    def __init__(self, opt=None,size=(8,16), hidden_size=512, n_mlp=8, style_dim=512, lr_mlp=0.01,
                 activation=None,
                 channel_multiplier=2,z=None, **kwargs):
        super(ImplicitGenerator_multiscale_, self).__init__()

        self.opt = opt
        if opt.apply_MOD_CLADE:
            self.approach = 0
        elif opt.only_CLADE:
            self.approach = 1
        elif opt.Matrix_Computation:
            self.approach = 2
        else:
            self.approach = -1

        self.tanh = nn.Tanh()

        self.starting_size = size  ##starting size!!
        demodulate = True
        self.demodulate = demodulate

        # print(size)
        self.emb = CIPblocks.ConstantInput(hidden_size, size=size)

        self.channels = {
            0: 1024,
            1: 512,
            2: 256,
            3: 128,
            4: 64,
            5: 32,
            6: 16,
            7: 8,
            8: 4,
            9: 2,
        }

        multiplier = 2




        ###kernel_size = 1===>first modFC layer!!only one layer!!input=embbed coords!!

        self.linears = nn.ModuleList()
        ##2xModFC for 2-8 Layers
        self.to_rgbs = nn.ModuleList()
        ##tRGB for 2-8 Layers

        self.log_size = int(CIPblocks.math.log(512, 2))
        ## 8 Layers

        self.n_blocks = int(self.log_size - CIPblocks.math.log(max(self.starting_size),2))
        self.size_dict = {
            i: (self.starting_size[0] * (2 ** i),self.starting_size[1] * (2 ** i)) for i in range(self.n_blocks+1)
        }
        ## in total n_intermediate blocks
        self.to_rgb_stride = 4
        ##how many ModFC between two tRGB==>in this case, 2 ModFC layers
        self.blocks = []
        self.coords = []

        self.coords = [(tt.convert_to_coord_format(opt.batch_size, self.size_dict[i][0],
                                                      self.size_dict[i][1],
                                                      integer_values=False))
                       for i in range(self.n_blocks+1)]

        self.lff = CIPblocks.LFF(hidden_size)

        for i in range(0, self.n_blocks):## for all blocks except final one
            block = nn.ModuleList()
            in_channels = self.channels[i]
            out_channels = self.channels[i+1]
            block.append(CIPblocks.StyledConv(in_channels,  ##the real in_channel 1024
                                              out_channels,  ##actually is out_channel
                                              1,
                                              style_dim,
                                              demodulate=demodulate,
                                              activation=activation,
                                              approach=self.approach,  # jhl
                                              ))
            block.append(CIPblocks.StyledConv(out_channels, out_channels, 1, style_dim,
                                           demodulate=demodulate, activation=activation,approach=self.approach,))#jhl
            block.append(CIPblocks.StyledConv(out_channels, out_channels, 1, style_dim,
                                           demodulate=demodulate, activation=activation,approach=self.approach,))#jhl
            block.append(
                CIPblocks.ToRGB(out_channels, style_dim, upsample=False, approach=self.approach, ))  # jhl
            block.append(CIPblocks.StyledConv(out_channels, int(out_channels/2), 1, style_dim,
                                                     demodulate=demodulate, activation=activation,
                                                     approach=self.approach, ))  # jhl
             ## don't need to specify param if you just want to define a layer!!!

            block.append(CIPblocks.LFF(int(out_channels/2)))
            self.blocks.append(block)


        self.final_block = nn.ModuleList()
        in_channels = out_channels
        out_channels = int(out_channels/2) ## the dimensions must be int !!!
        self.final_block.append(CIPblocks.StyledConv(in_channels,  ##the real in_channel 1024
                                               out_channels,  ##actually is out_channel
                                               1,
                                               style_dim,
                                               demodulate=demodulate,
                                               activation=activation,
                                               approach=self.approach,  # jhl
                                               ))
        self.final_block.append(CIPblocks.StyledConv(out_channels, out_channels, 1, style_dim,
                                               demodulate=demodulate, activation=activation,
                                               approach=self.approach, ))  # jhl
        self.final_block.append(CIPblocks.StyledConv(out_channels, out_channels, 1, style_dim,
                                               demodulate=demodulate, activation=activation,
                                               approach=self.approach, ))  # jhl
        self.final_block.append(
            CIPblocks.ToRGB(out_channels, style_dim, upsample=False, approach=self.approach, ))  # jhl



        self.style_dim = style_dim
        ##dimension of style vector

        layers = [CIPblocks.PixelNorm()]
        ##layers for latent normalization

        for i in range(n_mlp):##mapping network for style w(in total 8 layers)
            layers.append(
                CIPblocks.EqualLinear(
                    style_dim, style_dim, lr_mul=lr_mlp, activation='fused_lrelu'
                )
            )

        self.style = nn.Sequential(*layers)
        ##mapping network that generate style w!!




        self.styleMatrix = nn.Parameter(torch.randn(35,512))
        # self.styleMatrix.data.fill_(0.25)
        # self.alpha = nn.Parameter(torch.rand(1,512))
        # self.alpha.data.fill_(0.5)




    def forward(self,
                label,##[1,35,256,512]
                label_class_dict,
                coords,##[1,2,256,512]
                latent,##1D list[Tensor(1,512)]
                return_latents=False,
                truncation=1,
                truncation_latent=None,
                input_is_latent=False,
                edges=None,
                ):
        # print("input latent code:",latent)
        latent = latent[0]##[1,512]
        ##input noirse z
        # print("received latent[0] :",latent.shape,latent)
        if truncation < 1:
            latent = truncation_latent + truncation * (latent - truncation_latent)

        if not input_is_latent:
            latent = self.style(latent)

        x = self.lff(self.coords[0])
        ##Fourier Features:simple linear transformation with sin activation
            ##[N,512,256,512]
        # print(x)

        batch_size, _, h, w = coords.shape

        emb = self.emb(x)

        # print(x.shape,emb.shape)
        x = torch.cat([x, emb], 1)

        ##concatenation of Fourier Features and Coordinates Embeddings on channel dimension!!!
        ##[1,1024,256,512]

        rgb = 0

        label_all_res= [F.interpolate(label,size=self.size_dict[i],mode='nearest') for i in range(self.n_blocks+1)]
        label_class_dict_all_res = [torch.argmax(label_all_res[i],1) for i in range(self.n_blocks+1)]
        label_class_dict = label_class_dict_all_res[0]
        label = label_all_res[0]

        for i in range(self.n_blocks):  ## for all blocks except final one
            x = self.blocks[i][0](x, latent,label_class_dict=label_class_dict,label=label,class_style=self.styleMatrix,)
            x = self.blocks[i][1](x, latent,label_class_dict=label_class_dict,label=label,class_style=self.styleMatrix,)
            x = self.blocks[i][2](x, latent,label_class_dict=label_class_dict,label=label,class_style=self.styleMatrix,)
            rgb_original_size = self.blocks[i][3](x, latent, rgb,label_class_dict=label_class_dict,label=label,class_style=self.styleMatrix,)
            rbg = F.interpolate(rgb_original_size,scale_factor=2,mode = 'nearest')
            x = self.blocks[i][4](x, latent,label_class_dict=label_class_dict,label=label,class_style=self.styleMatrix,)
            x = F.interpolate(x,scale_factor=2,mode = 'nearest')
            label_class_dict = label_class_dict_all_res[i+1]
            label = label_all_res[i+1]
            fourier_features = self.blocks[i][5](self.coords[i+1])
            x = torch.cat((x,fourier_features),dim = 1)
        x = self.final_block[0](x, latent, label_class_dict=label_class_dict, label=label, class_style=self.styleMatrix, )
        x = self.final_block[1](x, latent, label_class_dict=label_class_dict, label=label, class_style=self.styleMatrix, )
        x = self.final_block[2](x, latent, label_class_dict=label_class_dict, label=label, class_style=self.styleMatrix, )
        rgb = self.final_block[3](x, latent, rgb, label_class_dict=label_class_dict, label=label,
                                 class_style=self.styleMatrix, )



        if return_latents:
            return rgb, latent
        else:
            return self.tanh(rgb), None



class ImplicitGenerator_multi_scale(nn.Module):




    def __init__(self, opt=None,size=256, hidden_size=512, n_mlp=8, style_dim=512, lr_mlp=0.01,
                 activation=None, channel_multiplier=2,z=None, **kwargs):
        super(ImplicitGenerator_multi_scale, self).__init__()

        self.opt = opt

        self.approach = 2

        self.tanh = nn.Tanh()

        self.size = size
        demodulate = True
        self.demodulate = demodulate
        self.lff0 = CIPblocks.LFF(hidden_size)
        self.lff1 = CIPblocks.LFF(int(hidden_size/2))
        self.lff2 = CIPblocks.LFF(int(hidden_size/4))
        self.lff3 = CIPblocks.LFF(int(hidden_size / 8))
        self.emb1 = CIPblocks.ConstantInput_multi_scale(hidden_size, size=size)
        # self.emb2 = CIPblocks.ConstantInput(hidden_size/2, size=size)
        # self.emb3 = CIPblocks.ConstantInput(hidden_size/4, size=size)


        self.channels = {
            0: 1024,
            1: 512,
            2: 256,
            3: 128,
            4: 64,
            5: 32,
            6: 16,

        }


        ###kernel_size = 1===>first modFC layer!!only one layer!!input=embbed coords!!

        self.linears = nn.ModuleList()
        ##2xModFC for 2-8 Layers
        self.to_rgbs = nn.ModuleList()

        self.coords0 = tt.convert_to_coord_format(opt.batch_size, 32, 64, integer_values=False)

        self.linears.append(CIPblocks.StyledConv(1024, 512, 1, style_dim,
                                                 demodulate=demodulate, activation=activation,
                                                 approach=self.approach, ))  # jhl
        self.linears.append(CIPblocks.StyledConv(512, 512, 1, style_dim,
                                                 demodulate=demodulate, activation=activation,
                                                 approach=self.approach, ))
        self.linears.append(CIPblocks.StyledConv(512, 512, 1, style_dim,
                                                 demodulate=demodulate, activation=activation,
                                                 approach=self.approach, ))
        self.linears.append(CIPblocks.StyledConv(512, 256, 1, style_dim,
                                                 demodulate=demodulate, activation=activation,
                                                 approach=self.approach, ))  # jhl

        self.linears.append(torch.nn.Upsample(scale_factor=2, mode='nearest',
                                              align_corners=None, recompute_scale_factor=None))




        self.coords1 = tt.convert_to_coord_format(opt.batch_size, 64, 128, integer_values=False)



        self.linears.append(CIPblocks.StyledConv(512, 256, 1, style_dim,
                            demodulate=demodulate, activation=activation,approach=self.approach, ))  # jhl
        self.linears.append(CIPblocks.StyledConv(256, 256, 1, style_dim,
                            demodulate=demodulate, activation=activation,approach=self.approach, ))
        self.linears.append(CIPblocks.StyledConv(256, 256, 1, style_dim,
                            demodulate=demodulate, activation=activation,approach=self.approach, ))
        self.linears.append(CIPblocks.StyledConv(256, 256 , 1, style_dim,
                            demodulate=demodulate, activation=activation,approach=self.approach, ))  # jhl

        self.linears.append(torch.nn.Upsample(scale_factor=2, mode='nearest',
                                              align_corners=None, recompute_scale_factor=None))
        self.coords2 = tt.convert_to_coord_format(opt.batch_size, 128, 256, integer_values=False)

        self.linears.append(CIPblocks.StyledConv(256, 128, 1, style_dim,
                            demodulate=demodulate, activation=activation,approach=self.approach, ))
        self.linears.append(CIPblocks.StyledConv(128, 128, 1, style_dim,
                            demodulate=demodulate, activation=activation,approach=self.approach, ))
        self.linears.append(CIPblocks.StyledConv(128, 128, 1, style_dim,
                            demodulate=demodulate, activation=activation,approach=self.approach,))#jhl
        self.linears.append(CIPblocks.StyledConv(128, 64, 1, style_dim,
                            demodulate=demodulate, activation=activation,approach=self.approach,))

        self.linears.append(torch.nn.Upsample(scale_factor=2, mode='nearest',
                          align_corners=None,recompute_scale_factor=None))
        self.coords3 = tt.convert_to_coord_format(opt.batch_size, 256, 512, integer_values=False)


        self.linears.append(CIPblocks.StyledConv(128, 64, 1, style_dim,
                            demodulate=demodulate, activation=activation,approach=self.approach, ))
        self.linears.append(CIPblocks.StyledConv(64, 64, 1, style_dim,
                            demodulate=demodulate, activation=activation,approach=self.approach, ))
        self.linears.append(CIPblocks.StyledConv(64, 64, 1, style_dim,
                            demodulate=demodulate, activation=activation,approach=self.approach, ))
        self.linears.append(CIPblocks.StyledConv(64, 64, 1, style_dim,
                            demodulate=demodulate, activation=activation,approach=self.approach, ))

        self.to_rgbs.append(CIPblocks.ToRGB(64, style_dim, upsample=False, approach=self.approach, ))

        self.style_dim = style_dim
        ##dimension of style vector

        layers = [CIPblocks.PixelNorm()]
        ##layers for latent normalization

        for i in range(n_mlp):##mapping network for style w(in total 8 layers)
            layers.append(
                CIPblocks.EqualLinear(
                    style_dim, style_dim, lr_mul=lr_mlp, activation='fused_lrelu'
                )
            )

        self.style = nn.Sequential(*layers)
        ##mapping network that generate style w!!




        self.styleMatrix = nn.Parameter(torch.randn(35,512))
        # self.styleMatrix.data.fill_(0.25)
        # self.alpha = nn.Parameter(torch.rand(1,512))
        # self.alpha.data.fill_(0.5)




    def forward(self,
                label,##[1,35,256,512]
                label_class_dict,
                coords,##[1,2,256,512]
                latent,##1D list[Tensor(1,512)]
                return_latents=False,
                truncation=1,
                truncation_latent=None,
                input_is_latent=False,
                edges=None,
                ):
        # print("input latent code:",latent)
        latent = latent[0]##[1,512]
        ##input noirse z
        # print("received latent[0] :",latent.shape,latent)
        if truncation < 1:
            latent = truncation_latent + truncation * (latent - truncation_latent)

        if not input_is_latent:
            latent = self.style(latent)
        ##style w [1,512]




        # latent = self.alpha*latent + (1-self.alpha)*self.styleMatrix
        ##combined style vector [35,512]

        ##Fourier Features:simple linear transformation with sin activation
            ##[N,512,256,512]
        # print(x)


        ##generate coordinate embedding for 256x512
        ##[1,512,256,512]


        ##concatenation of Fourier Features and Coordinates Embeddings on channel dimension!!!
        ##[1,1024,256,512]

        rgb = 0
        fourier_feature0 = self.lff0(self.coords0)  # [1,512,32,64]
        fourier_feature1 = self.lff1(self.coords1)#[1,256,64,128]
        fourier_feature2 = self.lff2(self.coords2)#[1,128,128,256]
        fourier_feature3 = self.lff3(self.coords3)#[1,64,256,512]
        x = self.emb1(fourier_feature0)

        x = torch.cat([fourier_feature0, x ], 1)

        label_32_64 = F.interpolate(label,  ##!!  interpolate can also downscale!!!
                              size=x.size()[2:],  ##take the H and W
                              mode='nearest')

        x = self.linears[0](x, latent,label_class_dict=label_class_dict,label=label_32_64,class_style=self.styleMatrix,)
        x = self.linears[1](x, latent, label_class_dict=label_class_dict, label=label_32_64, class_style=self.styleMatrix, )
        x = self.linears[2](x, latent, label_class_dict=label_class_dict, label=label_32_64, class_style=self.styleMatrix, )
        x = self.linears[3](x, latent, label_class_dict=label_class_dict, label=label_32_64, class_style=self.styleMatrix, )
        x = self.linears[4](x)
        x = torch.cat([fourier_feature1, x], 1)

        label_64_128 = F.interpolate(label,  ##!!  interpolate can also downscale!!!
                              size=x.size()[2:],  ##take the H and W
                              mode='nearest')

        x = self.linears[5](x, latent, label_class_dict=label_class_dict, label=label_64_128, class_style=self.styleMatrix, )
        x = self.linears[6](x, latent, label_class_dict=label_class_dict, label=label_64_128, class_style=self.styleMatrix, )
        x = self.linears[7](x, latent, label_class_dict=label_class_dict, label=label_64_128, class_style=self.styleMatrix, )
        x = self.linears[8](x, latent, label_class_dict=label_class_dict, label=label_64_128, class_style=self.styleMatrix, )
        x = self.linears[9](x)
        x = torch.cat([fourier_feature2, x], 1)

        label_128_256 = F.interpolate(label,  ##!!  interpolate can also downscale!!!
                              size=x.size()[2:],  ##take the H and W
                              mode='nearest')

        x = self.linears[10](x, latent, label_class_dict=label_class_dict, label=label_128_256, class_style=self.styleMatrix, )
        x = self.linears[11](x, latent, label_class_dict=label_class_dict, label=label_128_256, class_style=self.styleMatrix, )
        x = self.linears[12](x, latent, label_class_dict=label_class_dict, label=label_128_256, class_style=self.styleMatrix, )
        x = self.linears[13](x, latent, label_class_dict=label_class_dict, label=label_128_256, class_style=self.styleMatrix, )
        x = self.linears[14](x)

        x = torch.cat([fourier_feature3, x], 1)

        label_256_512 = label

        x = self.linears[15](x, latent, label_class_dict=label_class_dict, label=label_256_512,
                             class_style=self.styleMatrix, )
        x = self.linears[16](x, latent, label_class_dict=label_class_dict, label=label_256_512,
                             class_style=self.styleMatrix, )
        x = self.linears[17](x, latent, label_class_dict=label_class_dict, label=label_256_512,
                             class_style=self.styleMatrix, )
        x = self.linears[18](x, latent, label_class_dict=label_class_dict, label=label_256_512,
                             class_style=self.styleMatrix, )



        rgb = self.to_rgbs[0](x, latent, rgb,label_class_dict=label_class_dict,label=label_128_256,class_style=self.styleMatrix,)


        if return_latents:
            return rgb, latent
        else:

            # print("rgb size:",rgb.size())
            return self.tanh(rgb), None
            # return rgb, None








































class ResnetBlock_with_SPADE(nn.Module):
    def __init__(self, fin, fout, opt):
        super().__init__()
        self.opt = opt
        self.learned_shortcut = (fin != fout)
        self.add_edges = 1 if opt.add_edges else 0
        fmiddle = min(fin, fout)
        sp_norm = norms.get_spectral_norm(opt)
        self.conv_0 = sp_norm(nn.Conv2d(fin+self.add_edges, fmiddle, kernel_size=3, padding=1))
        self.conv_1 = sp_norm(nn.Conv2d(fmiddle+self.add_edges, fout, kernel_size=3, padding=1))
        if self.learned_shortcut:
            self.conv_s = sp_norm(nn.Conv2d(fin+self.add_edges, fout, kernel_size=1, bias=False))

        spade_conditional_input_dims = opt.semantic_nc
        if not opt.no_3dnoise:
            spade_conditional_input_dims += opt.z_dim

        self.norm_0 = norms.SPADE(opt, fin+self.add_edges, spade_conditional_input_dims)
        self.norm_1 = norms.SPADE(opt, fmiddle+self.add_edges, spade_conditional_input_dims)
        if self.learned_shortcut:
            self.norm_s = norms.SPADE(opt, fin+self.add_edges, spade_conditional_input_dims)
        self.activ = nn.LeakyReLU(0.2, inplace=True)

    def forward(self, x, seg, edges = None):


        if self.learned_shortcut:
            if self.add_edges:
                edges = F.interpolate(edges, size=x.shape[-2:])
                x = torch.cat([x, edges], dim=1)
            x_s = self.conv_s(self.norm_s(x, seg))
        else:
            x_s = x
            if self.add_edges:
                edges = F.interpolate(edges, size=x.shape[-2:])
                x = torch.cat([x, edges], dim=1)

        dx = self.conv_0(self.activ(self.norm_0(x, seg)))
        if self.add_edges :
            dx = torch.cat([dx,edges],dim = 1)
        dx = self.conv_1(self.activ(self.norm_1(dx, seg)))
        out = x_s + dx
        return out

class ResnetBlock_with_IWT_SPADE_HWT(nn.Module):
    def __init__(self, fin, fout, opt):
        super().__init__()
        self.opt = opt
        self.learned_shortcut = (fin != fout)
        fmiddle = min(fin, fout)
        sp_norm = norms.get_spectral_norm(opt)
        self.conv_0 = sp_norm(nn.Conv2d(fin, fmiddle, kernel_size=3, padding=1))
        self.conv_1 = sp_norm(nn.Conv2d(fmiddle, fout, kernel_size=3, padding=1))
        if self.learned_shortcut:
            self.conv_s = sp_norm(nn.Conv2d(fin, fout, kernel_size=1, bias=False))

        spade_conditional_input_dims = opt.semantic_nc
        if not opt.no_3dnoise:
            spade_conditional_input_dims += opt.z_dim

        self.norm_0 = norms.IWT_SPADE_HWT(opt, fin, spade_conditional_input_dims)
        self.norm_1 = norms.IWT_SPADE_HWT(opt, fmiddle, spade_conditional_input_dims)
        if self.learned_shortcut:
            self.norm_s = norms.IWT_SPADE_HWT(opt, fin, spade_conditional_input_dims)
        self.activ = nn.LeakyReLU(0.2, inplace=True)

    def forward(self, x, seg):
        if self.learned_shortcut:
            x_s = self.conv_s(self.norm_s(x, seg))
        else:
            x_s = x
        dx = self.conv_0(self.activ(self.norm_0(x, seg)))
        dx = self.conv_1(self.activ(self.norm_1(dx, seg)))
        out = x_s + dx
        return out

class ResBlock_with_SPADE(nn.Module):
    def __init__(self, fin, fout, opt):
        super().__init__()
        self.opt = opt
        self.add_edges = 1 if opt.add_edges else 0
        fmiddle = min(fin, fout)
        sp_norm = norms.get_spectral_norm(opt)
        self.conv_0 = sp_norm(nn.Conv2d(fin+self.add_edges, fmiddle, kernel_size=3, padding=1))
        self.conv_1 = sp_norm(nn.Conv2d(fmiddle+self.add_edges, fout, kernel_size=3, padding=1))

        spade_conditional_input_dims = opt.semantic_nc
        if not opt.no_3dnoise:
            spade_conditional_input_dims += opt.z_dim

        self.norm_0 = norms.SPADE(opt, fin+self.add_edges, spade_conditional_input_dims)
        self.norm_1 = norms.SPADE(opt, fmiddle+self.add_edges, spade_conditional_input_dims)

        self.activ = nn.LeakyReLU(0.2, inplace=True)

    def forward(self, x, seg, edges = None):

        if self.add_edges:
            edges = F.interpolate(edges, size=x.shape[-2:])
            x = torch.cat([x, edges], dim=1)
        dx = self.conv_0(self.activ(self.norm_0(x, seg)))
        if self.add_edges :
            dx = torch.cat([dx,edges],dim = 1)
        dx = self.conv_1(self.activ(self.norm_1(dx, seg)))
        out = dx
        return out

class Upsample(nn.Module):
    def __init__(self, kernel, factor=2):
        super().__init__()

        self.factor = factor
        kernel = make_kernel(kernel) * (factor ** 2)
        self.register_buffer("kernel", kernel)

        p = kernel.shape[0] - factor

        pad0 = (p + 1) // 2 + factor - 1
        pad1 = p // 2

        self.pad = (pad0, pad1)

    def forward(self, input):
        out = upfirdn2d(input, self.kernel, up=self.factor, down=1, pad=self.pad)

        return out

class ToRGB(nn.Module):
    def __init__(self, in_channel,opt, upsample=True, blur_kernel=[1, 3, 3, 1]):
        super().__init__()
        sp_norm = norms.get_spectral_norm(opt)

        if upsample:
            self.upsample = nn.Upsample(scale_factor = 2)

        self.conv = sp_norm(nn.Conv2d(in_channel, 3, 1, 1,padding_mode='reflect'))

    def forward(self, input, skip=None):
        out = self.conv(F.leaky_relu(input,2e1))

        if skip is not None:
            skip = self.upsample(skip)

            out = out + skip

        return out


class wavelet_generator(nn.Module):
    def __init__(self, opt):
        super().__init__()
        self.opt = opt
        sp_norm = norms.get_spectral_norm(opt)
        ch = opt.channels_G
        self.channels = [4*16*ch, 4*16*ch, 4*16*ch, 4*8*ch, 4*4*ch, 4*2*ch, 4*1*ch]
        self.init_W, self.init_H = self.compute_latent_vector_size(opt)
        self.init_W = self.init_W // 2
        self.init_H = self.init_H // 2
        #self.conv_img = nn.Conv2d(self.channels[-1], 3, 3, padding=1)
        self.conv_img = ToRGB_wavelet(self.channels[-1])
        self.up = nn.Upsample(scale_factor=2)
        self.body = nn.ModuleList([])
        if self.opt.progressive_growing :
            self.torgbs = nn.ModuleList([])
        for i in range(len(self.channels)-1):
            self.body.append(ResnetBlock_with_SPADE(self.channels[i], self.channels[i+1], opt))
            if self.opt.progressive_growing:
                self.torgbs.append(ToRGB_wavelet(in_channel=self.channels[i+1]))
        if not self.opt.no_3dnoise:
            self.fc = nn.Conv2d(self.opt.semantic_nc + self.opt.z_dim, 4 * 16 * ch, 3, padding=1)
        else:
            self.fc = nn.Conv2d(self.opt.semantic_nc, 4 * 16 * ch, 3, padding=1)

        self.iwt = InverseHaarTransform(3)


    def compute_latent_vector_size(self, opt):
        w = opt.crop_size // (2**(opt.num_res_blocks-1))
        h = round(w / opt.aspect_ratio)
        return h, w

    def forward(self, input, z=None,edges = None):
        seg = input
        if self.opt.gpu_ids != "-1":
            seg.cuda()
        if not self.opt.no_3dnoise:
            dev = seg.get_device() if self.opt.gpu_ids != "-1" else "cpu"
            z = torch.randn(seg.size(0), self.opt.z_dim, dtype=torch.float32, device=dev)
            z = z.view(z.size(0), self.opt.z_dim, 1, 1)
            z = z.expand(z.size(0), self.opt.z_dim, seg.size(2), seg.size(3))
            seg = torch.cat((z, seg), dim = 1)
        x = F.interpolate(seg, size=(self.init_W, self.init_H))
        x = self.fc(x)
        out = None
        for i in range(self.opt.num_res_blocks):
            x = self.body[i](x, seg)

            if self.opt.progressive_growing and out == None :
                out = self.torgbs[i](x)
            elif self.opt.progressive_growing :
                out = self.torgbs[i](x,skip = out)

            if i < self.opt.num_res_blocks-1:
                x = self.up(x)

        if self.opt.progressive_growing :
            x = out
        else :
            x = self.conv_img(x)

        x = self.iwt(x)
        x = F.tanh(x)

        return x

class ToRGB_wavelet(nn.Module):
    def __init__(self, in_channel, upsample=True, blur_kernel=[1, 3, 3, 1]):
        super().__init__()

        if upsample:
            self.iwt = InverseHaarTransform(3)
            self.upsample = Upsample(blur_kernel)
            self.dwt = HaarTransform(3)

        self.conv = nn.Conv2d(in_channel, 3 * 4,1,1)

    def forward(self, input, skip=None):
        out = self.conv(input)

        if skip is not None:
            skip = self.iwt(skip)
            skip = self.upsample(skip)
            skip = self.dwt(skip)

            out = out + skip

        return out

class WaveletUpsample(nn.Module):
    def __init__(self, factor=2):
        super().__init__()
        if factor != 2 :
            print('wavelet upsampling is not implemented for factors different than 2')

        self.factor = factor
        self.w = torch.zeros(self.factor, self.factor)
        self.w[0, 0] = 1



    def forward(self, input):
        output = F.conv_transpose2d(input, self.w.expand(input.size(1), 1, self.factor, self.factor),
                                  stride=self.factor, groups=input.size(1))
        output[...,0:input.size(-2),0:input.size(-1)]=2*input
        return output


class WaveletUpsample2(nn.Module):
    def __init__(self, factor=2):
        super().__init__()
        if factor != 2 :
            print('wavelet upsampling is not implemented for factors different than 2')

        self.factor = factor
        self.upsample = nn.Upsample(scale_factor=2,mode='nearest')




    def forward(self, input):
        output = self.upsample(input)
        output[...,0:input.size(-2),0:input.size(-1)]=2*input
        return output


class WaveletUpsampleChannels(nn.Module):
    def __init__(self, factor=2):
        super().__init__()
        if factor != 2 :
            print('wavelet upsampling is not implemented for factors different than 2')

        self.factor = factor
        self.w = torch.zeros(self.factor, self.factor).cuda()
        self.w[0, 0] = 1

        self.iwt = InverseHaarTransform(3)



    def forward(self, input):
        ll, lh, hl, hh = input.chunk(4, 1)

        ll_out = self.iwt(input)
        lh_out = F.conv_transpose2d(lh, self.w.expand(lh.size(1), 1, self.factor, self.factor),
                                  stride=self.factor, groups=lh.size(1))
        hl_out = F.conv_transpose2d(hl, self.w.expand(hl.size(1), 1, self.factor, self.factor),
                                    stride=self.factor, groups=hl.size(1))
        hh_out = F.conv_transpose2d(hh, self.w.expand(hh.size(1), 1, self.factor, self.factor),
                                    stride=self.factor, groups=hh.size(1))
        output=torch.cat((ll_out,lh_out,hl_out,hh_out),dim=1)

        return output

class ReductiveWaveletUpsampleChannels(nn.Module):
    def __init__(self, factor=2):
        super().__init__()
        if factor != 2 :
            print('wavelet upsampling is not implemented for factors different than 2')

        self.factor = factor

        self.iwt = InverseHaarTransform(3)



    def forward(self, input):

        ll_out = self.iwt(input)

        output=ll_out

        return output

class IWT_Upsample_HWT(nn.Module):
    def __init__(self, factor=2,mode='nearest'):
        super().__init__()
        if factor != 2 :
            print('wavelet upsampling is not implemented for factors different than 2')

        self.factor = factor

        self.iwt = InverseHaarTransform(3)
        self.up = nn.Upsample(scale_factor=factor,mode=mode)
        self.hwt = HaarTransform(3)



    def forward(self, input):

        output = self.iwt(input)
        output = self.up(output)
        output = self.hwt(output)

        return output


class wavelet_generator_multiple_levels(nn.Module):
    def __init__(self, opt):
        super().__init__()
        self.opt = opt
        sp_norm = norms.get_spectral_norm(opt)
        ch = opt.channels_G
        self.channels = [4*16*ch, 4*16*ch, 4*16*ch, 4*8*ch, 4*4*ch, 4*2*ch, 4*1*ch]
        self.init_W, self.init_H = self.compute_latent_vector_size(opt)
        self.init_W = self.init_W // 2
        self.init_H = self.init_H // 2
        #self.conv_img = nn.Conv2d(self.channels[-1], 3, 3, padding=1)
        self.conv_img = ToRGB_wavelet(self.channels[-1])
        self.up = WaveletUpsampleChannels(factor=2)
        self.body = nn.ModuleList([])
        if self.opt.progressive_growing :
            self.torgbs = nn.ModuleList([])
        for i in range(len(self.channels)-1):
            self.body.append(ResnetBlock_with_SPADE(self.channels[i], self.channels[i+1], opt))
            if self.opt.progressive_growing:
                self.torgbs.append(ToRGB_wavelet(in_channel=self.channels[i+1]))
        if not self.opt.no_3dnoise:
            self.fc = nn.Conv2d(self.opt.semantic_nc + self.opt.z_dim, 4 * 16 * ch, 3, padding=1)
        else:
            self.fc = nn.Conv2d(self.opt.semantic_nc, 4 * 16 * ch, 3, padding=1)

        self.iwt = InverseHaarTransform(3)


    def compute_latent_vector_size(self, opt):
        w = opt.crop_size // (2**(opt.num_res_blocks-1))
        h = round(w / opt.aspect_ratio)
        return h, w

    def forward(self, input, z=None,edges = None):
        seg = input
        if self.opt.gpu_ids != "-1":
            seg.cuda()
        if not self.opt.no_3dnoise:
            dev = seg.get_device() if self.opt.gpu_ids != "-1" else "cpu"
            z = torch.randn(seg.size(0), self.opt.z_dim, dtype=torch.float32, device=dev)
            z = z.view(z.size(0), self.opt.z_dim, 1, 1)
            z = z.expand(z.size(0), self.opt.z_dim, seg.size(2), seg.size(3))
            seg = torch.cat((z, seg), dim = 1)
        x = F.interpolate(seg, size=(self.init_W, self.init_H))
        x = self.fc(x)
        out = None
        for i in range(self.opt.num_res_blocks):
            x = self.body[i](x, seg)

            if self.opt.progressive_growing and out == None :
                out = self.torgbs[i](x)
            elif self.opt.progressive_growing :
                out = self.torgbs[i](x,skip = out)

            if i < self.opt.num_res_blocks-1:
                x = self.up(x)

        if self.opt.progressive_growing :
            x = out
        else :
            x = self.conv_img(x)

        x = self.iwt(x)
        x = F.tanh(x)

        return x

class wavelet_generator_multiple_levels_no_tanh(nn.Module):
    def __init__(self, opt):
        super().__init__()
        self.opt = opt
        sp_norm = norms.get_spectral_norm(opt)
        ch = opt.channels_G
        self.channels = [4*16*ch, 4*16*ch, 4*16*ch, 4*8*ch, 4*4*ch, 4*2*ch, 4*1*ch]
        self.init_W, self.init_H = self.compute_latent_vector_size(opt)
        self.init_W = self.init_W // 2
        self.init_H = self.init_H // 2
        #self.conv_img = nn.Conv2d(self.channels[-1], 3, 3, padding=1)
        self.conv_img = ToRGB_wavelet(self.channels[-1])
        self.up = WaveletUpsampleChannels(factor=2)
        self.body = nn.ModuleList([])
        if self.opt.progressive_growing :
            self.torgbs = nn.ModuleList([])
        for i in range(len(self.channels)-1):
            self.body.append(ResnetBlock_with_SPADE(self.channels[i], self.channels[i+1], opt))
            if self.opt.progressive_growing:
                self.torgbs.append(ToRGB_wavelet(in_channel=self.channels[i+1]))
        if not self.opt.no_3dnoise:
            self.fc = nn.Conv2d(self.opt.semantic_nc + self.opt.z_dim, 4 * 16 * ch, 3, padding=1)
        else:
            self.fc = nn.Conv2d(self.opt.semantic_nc, 4 * 16 * ch, 3, padding=1)

        self.iwt = InverseHaarTransform(3)


    def compute_latent_vector_size(self, opt):
        w = opt.crop_size // (2**(opt.num_res_blocks-1))
        h = round(w / opt.aspect_ratio)
        return h, w

    def forward(self, input, z=None,edges = None):
        seg = input
        if self.opt.gpu_ids != "-1":
            seg.cuda()
        if not self.opt.no_3dnoise:
            dev = seg.get_device() if self.opt.gpu_ids != "-1" else "cpu"
            z = torch.randn(seg.size(0), self.opt.z_dim, dtype=torch.float32, device=dev)
            z = z.view(z.size(0), self.opt.z_dim, 1, 1)
            z = z.expand(z.size(0), self.opt.z_dim, seg.size(2), seg.size(3))
            seg = torch.cat((z, seg), dim = 1)
        x = F.interpolate(seg, size=(self.init_W, self.init_H))
        x = self.fc(x)
        out = None
        for i in range(self.opt.num_res_blocks):
            x = self.body[i](x, seg)

            if self.opt.progressive_growing and out == None :
                out = self.torgbs[i](x)
            elif self.opt.progressive_growing :
                out = self.torgbs[i](x,skip = out)

            if i < self.opt.num_res_blocks-1:
                x = self.up(x)

        if self.opt.progressive_growing :
            x = out
        else :
            x = self.conv_img(x)

        x = self.iwt(x)

        return x

class IWT_spade_upsample_WT_generator(nn.Module):
    def __init__(self, opt):
        super().__init__()
        self.opt = opt
        sp_norm = norms.get_spectral_norm(opt)
        ch = opt.channels_G
        self.channels = [4*16*ch, 4*16*ch, 4*16*ch, 4*8*ch, 4*4*ch, 4*2*ch, 4*1*ch]
        self.init_W, self.init_H = self.compute_latent_vector_size(opt)
        self.init_W = self.init_W // 2
        self.init_H = self.init_H // 2
        #self.conv_img = nn.Conv2d(self.channels[-1], 3, 3, padding=1)
        self.conv_img = ToRGB_wavelet(self.channels[-1])
        self.up = IWT_Upsample_HWT(factor=2)
        self.body = nn.ModuleList([])
        if self.opt.progressive_growing :
            self.torgbs = nn.ModuleList([])
        for i in range(len(self.channels)-1):
            self.body.append(ResnetBlock_with_IWT_SPADE_HWT(self.channels[i], self.channels[i+1], opt))
            if self.opt.progressive_growing:
                self.torgbs.append(ToRGB_wavelet(in_channel=self.channels[i+1]))
        if not self.opt.no_3dnoise:
            self.fc = nn.Conv2d(self.opt.semantic_nc + self.opt.z_dim, 4 * 16 * ch, 3, padding=1)
        else:
            self.fc = nn.Conv2d(self.opt.semantic_nc, 4 * 16 * ch, 3, padding=1)

        self.iwt = InverseHaarTransform(3)


    def compute_latent_vector_size(self, opt):
        w = opt.crop_size // (2**(opt.num_res_blocks-1))
        h = round(w / opt.aspect_ratio)
        return h, w

    def forward(self, input, z=None,edges = None):
        seg = input
        if self.opt.gpu_ids != "-1":
            seg.cuda()
        if not self.opt.no_3dnoise:
            dev = seg.get_device() if self.opt.gpu_ids != "-1" else "cpu"
            z = torch.randn(seg.size(0), self.opt.z_dim, dtype=torch.float32, device=dev)
            z = z.view(z.size(0), self.opt.z_dim, 1, 1)
            z = z.expand(z.size(0), self.opt.z_dim, seg.size(2), seg.size(3))
            seg = torch.cat((z, seg), dim = 1)
        x = F.interpolate(seg, size=(self.init_W, self.init_H))
        x = self.fc(x)
        out = None
        for i in range(self.opt.num_res_blocks):
            x = self.body[i](x, seg)

            if self.opt.progressive_growing and out == None :
                out = self.torgbs[i](x)
            elif self.opt.progressive_growing :
                out = self.torgbs[i](x,skip = out)

            if i < self.opt.num_res_blocks-1:
                x = self.up(x)

        if self.opt.progressive_growing :
            x = out
        else :
            x = self.conv_img(x)

        x = self.iwt(x)
        x = F.tanh(x)

        return x

class wavelet_generator_multiple_levels_reductive_upsample(nn.Module):
    def __init__(self, opt):
        super().__init__()
        self.opt = opt
        sp_norm = norms.get_spectral_norm(opt)
        ch = opt.channels_G
        self.channels = [4*4*16*ch, 4*16*ch, 4*16*ch, 4*8*ch, 4*4*ch, 4*2*ch, 4*1*ch]
        self.init_W, self.init_H = self.compute_latent_vector_size(opt)
        self.init_W = self.init_W // 2
        self.init_H = self.init_H // 2
        #self.conv_img = nn.Conv2d(self.channels[-1], 3, 3, padding=1)
        self.conv_img = ToRGB_wavelet(self.channels[-1])
        self.up = ReductiveWaveletUpsampleChannels(factor=2)
        self.body = nn.ModuleList([])
        if self.opt.progressive_growing :
            self.torgbs = nn.ModuleList([])
        for i in range(len(self.channels)-1):
            self.body.append(ResnetBlock_with_SPADE(self.channels[i]//4, self.channels[i+1], opt))
            if self.opt.progressive_growing:
                self.torgbs.append(ToRGB_wavelet(in_channel=self.channels[i+1]))
        if not self.opt.no_3dnoise:
            self.fc = nn.Conv2d(self.opt.semantic_nc + self.opt.z_dim, 4 * 16 * ch, 3, padding=1)
        else:
            self.fc = nn.Conv2d(self.opt.semantic_nc, 4 * 16 * ch, 3, padding=1)

        self.iwt = InverseHaarTransform(3)


    def compute_latent_vector_size(self, opt):
        w = opt.crop_size // (2**(opt.num_res_blocks-1))
        h = round(w / opt.aspect_ratio)
        return h, w

    def forward(self, input, z=None,edges = None):
        seg = input
        if self.opt.gpu_ids != "-1":
            seg.cuda()
        if not self.opt.no_3dnoise:
            dev = seg.get_device() if self.opt.gpu_ids != "-1" else "cpu"
            z = torch.randn(seg.size(0), self.opt.z_dim, dtype=torch.float32, device=dev)
            z = z.view(z.size(0), self.opt.z_dim, 1, 1)
            z = z.expand(z.size(0), self.opt.z_dim, seg.size(2), seg.size(3))
            seg = torch.cat((z, seg), dim = 1)
        x = F.interpolate(seg, size=(self.init_W, self.init_H))
        x = self.fc(x)
        out = None
        for i in range(self.opt.num_res_blocks):
            x = self.body[i](x, seg)

            if self.opt.progressive_growing and out == None :
                out = self.torgbs[i](x)
            elif self.opt.progressive_growing :
                out = self.torgbs[i](x,skip = out)

            if i < self.opt.num_res_blocks-1:
                x = self.up(x)

        if self.opt.progressive_growing :
            x = out
        else :
            x = self.conv_img(x)

        x = self.iwt(x)

        return x




class IWT_spade_upsample_WT_reductive_upsample_generator(nn.Module):
    def __init__(self, opt):
        super().__init__()
        self.opt = opt
        sp_norm = norms.get_spectral_norm(opt)
        ch = opt.channels_G
        self.channels = [4*4*16*ch, 4*16*ch, 4*16*ch, 4*8*ch, 4*4*ch, 4*2*ch, 4*1*ch]
        self.init_W, self.init_H = self.compute_latent_vector_size(opt)
        self.init_W = self.init_W // 2
        self.init_H = self.init_H // 2
        #self.conv_img = nn.Conv2d(self.channels[-1], 3, 3, padding=1)
        self.conv_img = ToRGB_wavelet(self.channels[-1])
        self.up = ReductiveWaveletUpsampleChannels(factor=2)
        self.body = nn.ModuleList([])
        if self.opt.progressive_growing :
            self.torgbs = nn.ModuleList([])
        for i in range(len(self.channels)-1):
            self.body.append(ResnetBlock_with_IWT_SPADE_HWT(self.channels[i]//4, self.channels[i+1], opt))
            if self.opt.progressive_growing:
                self.torgbs.append(ToRGB_wavelet(in_channel=self.channels[i+1]))
        if not self.opt.no_3dnoise:
            self.fc = nn.Conv2d(self.opt.semantic_nc + self.opt.z_dim, 4 * 16 * ch, 3, padding=1)
        else:
            self.fc = nn.Conv2d(self.opt.semantic_nc, 4 * 16 * ch, 3, padding=1)

        self.iwt = InverseHaarTransform(3)


    def compute_latent_vector_size(self, opt):
        w = opt.crop_size // (2**(opt.num_res_blocks-1))
        h = round(w / opt.aspect_ratio)
        return h, w

    def forward(self, input, z=None,edges = None):
        seg = input
        if self.opt.gpu_ids != "-1":
            seg.cuda()
        if not self.opt.no_3dnoise:
            dev = seg.get_device() if self.opt.gpu_ids != "-1" else "cpu"
            z = torch.randn(seg.size(0), self.opt.z_dim, dtype=torch.float32, device=dev)
            z = z.view(z.size(0), self.opt.z_dim, 1, 1)
            z = z.expand(z.size(0), self.opt.z_dim, seg.size(2), seg.size(3))
            seg = torch.cat((z, seg), dim = 1)
        x = F.interpolate(seg, size=(self.init_W, self.init_H))
        x = self.fc(x)
        out = None
        for i in range(self.opt.num_res_blocks):
            x = self.body[i](x, seg)

            if self.opt.progressive_growing and out == None :
                out = self.torgbs[i](x)
            elif self.opt.progressive_growing :
                out = self.torgbs[i](x,skip = out)

            if i < self.opt.num_res_blocks-1:
                x = self.up(x)

        if self.opt.progressive_growing :
            x = out
        else :
            x = self.conv_img(x)

        x = self.iwt(x)
        x = F.tanh(x)

        return x

class progGrow_Generator(nn.Module):
    def __init__(self, opt):
        super().__init__()
        self.opt = opt
        sp_norm = norms.get_spectral_norm(opt)
        ch = opt.channels_G
        self.channels = [16*ch, 16*ch, 16*ch, 8*ch, 4*ch, 2*ch, 1*ch]
        self.init_W, self.init_H = self.compute_latent_vector_size(opt)
        self.conv_img = nn.Conv2d(self.channels[-1], 3, 3, padding=1)
        self.add_edges = 1 if opt.add_edges else 0
        #self.conv_img =
        # (self.channels[-1])
        self.up = nn.Upsample(scale_factor=2)
        self.body = nn.ModuleList([])
        if self.opt.progressive_growing :
            self.torgbs = nn.ModuleList([])
        for i in range(len(self.channels)-1):
            self.body.append(ResBlock_with_SPADE(self.channels[i], self.channels[i+1], opt))
            if self.opt.progressive_growing:
                self.torgbs.append(progGrow_ToRGB(in_channel=self.channels[i+1],opt = opt))
        """        if not self.opt.no_3dnoise:
            self.fc = nn.Conv2d(self.opt.semantic_nc + self.opt.z_dim+self.add_edges, 16 * ch, 3, padding=1)
        else:
            self.fc = nn.Conv2d(self.opt.semantic_nc+self.add_edges, 16 * ch, 3, padding=1)"""

        self.constant_input = ConstantInput(self.channels[0],(self.init_W, self.init_H))

    def compute_latent_vector_size(self, opt):
        w = opt.crop_size // (2**(opt.num_res_blocks-1))
        h = round(w / opt.aspect_ratio)
        return h, w

    def forward(self, input, z=None,edges = None):
        seg = input
        if self.opt.gpu_ids != "-1":
            seg.cuda()
        if not self.opt.no_3dnoise:
            dev = seg.get_device() if self.opt.gpu_ids != "-1" else "cpu"
            z = torch.randn(seg.size(0), self.opt.z_dim, dtype=torch.float32, device=dev)
            z = z.view(z.size(0), self.opt.z_dim, 1, 1)
            z = z.expand(z.size(0), self.opt.z_dim, seg.size(2), seg.size(3))
            seg = torch.cat((z, seg), dim = 1)
        if self.add_edges :
            x = F.interpolate(torch.cat((seg,edges),dim = 1), size=(self.init_W, self.init_H))
        else :
            x = F.interpolate(seg, size=(self.init_W, self.init_H))
        x = self.constant_input(seg)
        #x = self.fc(x)
        out = None
        for i in range(self.opt.num_res_blocks):
            x = self.body[i](x, seg,edges)

            if self.opt.progressive_growing and out == None :
                out = self.torgbs[i](x,seg)
            elif self.opt.progressive_growing :
                out = self.torgbs[i](x,seg,skip = out)

            if i < self.opt.num_res_blocks-1:
                x = self.up(x)

        if self.opt.progressive_growing :
            x = out
            #x = F.tanh(x)
        else :
            x = self.conv_img(F.leaky_relu(x, 2e-1))
            x = F.tanh(x)

        return x



class progGrow_ToRGB(nn.Module):
    def __init__(self, in_channel,opt, upsample=True, blur_kernel=[1, 3, 3, 1]):
        super().__init__()
        sp_norm = norms.get_spectral_norm(opt)

        if upsample:
            self.upsample = nn.Upsample(scale_factor = 2,mode='bilinear')

        self.conv = nn.Conv2d(in_channel, 3, 1, 1)

        spade_conditional_input_dims = opt.semantic_nc
        if not opt.no_3dnoise:
            spade_conditional_input_dims += opt.z_dim

        self.norm = norms.SPADE(opt, in_channel, spade_conditional_input_dims)
        self.activ = nn.LeakyReLU(0.2, inplace=True)

    def forward(self, input,seg, skip=None):
        out = self.conv(input)

        if skip is not None:
            skip = self.upsample(skip)
            out = out + skip

        return out


class ConstantInput(nn.Module):
    def __init__(self, channel, size=(8,4)):
        super().__init__()

        self.input = nn.Parameter(torch.randn(1, channel, *ƒsize))

    def forward(self, input):
        batch = input.shape[0]
        out = self.input.repeat(batch, 1, 1, 1)

        return out

class ResidualWaveletGenerator(nn.Module):
    def __init__(self, opt):
        super().__init__()
        self.opt = opt
        sp_norm = norms.get_spectral_norm(opt)
        ch = opt.channels_G
        self.channels = [4*16*ch, 4*16*ch, 4*16*ch, 4*8*ch, 4*4*ch, 4*2*ch, 4*1*ch]

        self.init_W, self.init_H = self.compute_latent_vector_size(opt)

        self.conv_img = ToRGB_wavelet(in_channel=self.channels[-1],upsample = False)
        self.iwt = InverseHaarTransform(3)

        self.up = nn.Upsample(scale_factor=2)
        self.up_residual = IWT_Upsample_HWT(factor=2,mode='bilinear')
        self.body = nn.ModuleList([])
        for i in range(len(self.channels)-1):
            self.body.append(WaveletBlock_with_SPADE(self.channels[i], self.channels[i+1], opt))

        if not self.opt.no_3dnoise:
            self.fc = nn.Conv2d(self.opt.semantic_nc + self.opt.z_dim, 4*16 * ch, 3, padding=1)
        else:
            self.fc = nn.Conv2d(self.opt.semantic_nc, 4*16 * ch, 3, padding=1)

#        self.constant_input = ConstantInput(self.channels[0],(self.init_W, self.init_H))

    def compute_latent_vector_size(self, opt):
        w = opt.crop_size // (2**(opt.num_res_blocks-1))
        h = round(w / opt.aspect_ratio)
        return h//2, w//2

    def forward(self, input, z=None,edges = None):
        seg = input
        if self.opt.gpu_ids != "-1":
            seg.cuda()
        if not self.opt.no_3dnoise:
            dev = seg.get_device() if self.opt.gpu_ids != "-1" else "cpu"
            z = torch.randn(seg.size(0), self.opt.z_dim, dtype=torch.float32, device=dev)
            z = z.view(z.size(0), self.opt.z_dim, 1, 1)
            z = z.expand(z.size(0), self.opt.z_dim, seg.size(2), seg.size(3))
            seg = torch.cat((z, seg), dim = 1)


        x = F.interpolate(seg, size=(self.init_W, self.init_H))

        x = self.fc(x)
        for i in range(self.opt.num_res_blocks):
            x_s,x = self.body[i](x, seg,edges)

            if i < self.opt.num_res_blocks-1:
                x = self.up(x)
                x_s = self.up_residual(x_s)
                x = x+x_s


        x = self.conv_img(x+x_s)
        x = self.iwt(x)
        x = F.tanh(x)

        return x

class WaveletBlock_with_SPADE(nn.Module):
    def __init__(self, fin, fout, opt):
        super().__init__()
        self.opt = opt
        self.learned_shortcut = (fin != fout)
        fmiddle = min(fin, fout)
        sp_norm = norms.get_spectral_norm(opt)
        self.conv_0 = sp_norm(nn.Conv2d(fin, fmiddle, kernel_size=3, padding=1))
        self.conv_1 = sp_norm(nn.Conv2d(fmiddle, fout, kernel_size=3, padding=1))
        if self.learned_shortcut:
            self.conv_s = sp_norm(nn.Conv2d(fin, fout, kernel_size=1, bias=False))

        spade_conditional_input_dims = opt.semantic_nc
        if not opt.no_3dnoise:
            spade_conditional_input_dims += opt.z_dim

        self.norm_0 = norms.SPADE(opt, fin, spade_conditional_input_dims)
        self.norm_1 = norms.SPADE(opt, fmiddle, spade_conditional_input_dims)
        if self.learned_shortcut:
            self.norm_s = norms.SPADE(opt, fin, spade_conditional_input_dims)
        self.activ = nn.LeakyReLU(0.2, inplace=True)

    def forward(self, x, seg, edges = None):


        if self.learned_shortcut:
            #x_s = self.conv_s(x)
            x_s = self.conv_s(self.norm_s(x, seg))
        else:
            x_s = x

        dx = self.conv_0(self.activ(self.norm_0(x, seg)))
        dx = self.conv_1(self.activ(self.norm_1(dx, seg)))
        return x_s,dx


class ResidualWaveletGenerator_1(nn.Module):
    def __init__(self, opt):
        super().__init__()
        self.opt = opt
        sp_norm = norms.get_spectral_norm(opt)
        ch = opt.channels_G
        self.channels = [4*16*ch, 4*16*ch, 4*16*ch, 4*8*ch, 4*4*ch, 4*2*ch, 4*1*ch]

        self.init_W, self.init_H = self.compute_latent_vector_size(opt)

        self.conv_img = ToRGB_wavelet(in_channel=self.channels[-1],upsample = False)
        self.iwt = InverseHaarTransform(3)

        self.up = nn.Upsample(scale_factor=2)
        self.up_residual = IWT_Upsample_HWT(factor=2,mode='bilinear')
        self.body = nn.ModuleList([])
        for i in range(len(self.channels)-1):
            self.body.append(WaveletBlock_with_IWT_SPADE_HWT(self.channels[i], self.channels[i+1], opt))

        if not self.opt.no_3dnoise:
            self.fc = nn.Conv2d(self.opt.semantic_nc + self.opt.z_dim, 4*16 * ch, 3, padding=1)
        else:
            self.fc = nn.Conv2d(self.opt.semantic_nc, 4*16 * ch, 3, padding=1)

#        self.constant_input = ConstantInput(self.channels[0],(self.init_W, self.init_H))

    def compute_latent_vector_size(self, opt):
        w = opt.crop_size // (2**(opt.num_res_blocks-1))
        h = round(w / opt.aspect_ratio)
        return h//2, w//2

    def forward(self, input, z=None,edges = None):
        seg = input
        if self.opt.gpu_ids != "-1":
            seg.cuda()
        if not self.opt.no_3dnoise:
            dev = seg.get_device() if self.opt.gpu_ids != "-1" else "cpu"
            z = torch.randn(seg.size(0), self.opt.z_dim, dtype=torch.float32, device=dev)
            z = z.view(z.size(0), self.opt.z_dim, 1, 1)
            z = z.expand(z.size(0), self.opt.z_dim, seg.size(2), seg.size(3))
            seg = torch.cat((z, seg), dim = 1)


        x = F.interpolate(seg, size=(self.init_W, self.init_H))

        x = self.fc(x)
        for i in range(self.opt.num_res_blocks):
            x_s,x = self.body[i](x, seg,edges)

            if i < self.opt.num_res_blocks-1:
                x = self.up(x)
                x_s = self.up_residual(x_s)
                x = x+x_s


        x = self.conv_img(x+x_s)
        x = self.iwt(x)
        x = F.tanh(x)

        return x

    def forward_determinstic(self, input, noise_vector):
        seg = input
        edges = None
        if self.opt.gpu_ids != "-1":
            seg.cuda()
        if not self.opt.no_3dnoise:
            dev = seg.get_device() if self.opt.gpu_ids != "-1" else "cpu"
            z = noise_vector.to(dev)
            seg = torch.cat((z, seg), dim = 1)


        x = F.interpolate(seg, size=(self.init_W, self.init_H))

        x = self.fc(x)
        for i in range(self.opt.num_res_blocks):
            x_s,x = self.body[i](x, seg,edges)

            if i < self.opt.num_res_blocks-1:
                x = self.up(x)
                x_s = self.up_residual(x_s)
                x = x+x_s


        x = self.conv_img(x+x_s)
        x = self.iwt(x)
        x = F.tanh(x)

        return x


class WaveletBlock_with_IWT_SPADE_HWT(nn.Module):
    def __init__(self, fin, fout, opt):
        super().__init__()
        self.opt = opt
        self.learned_shortcut = (fin != fout)
        fmiddle = min(fin, fout)
        sp_norm = norms.get_spectral_norm(opt)
        self.conv_0 = sp_norm(nn.Conv2d(fin, fmiddle, kernel_size=3, padding=1))
        self.conv_1 = sp_norm(nn.Conv2d(fmiddle, fout, kernel_size=3, padding=1))
        if self.learned_shortcut:
            self.conv_s = sp_norm(nn.Conv2d(fin, fout, kernel_size=1, bias=False))

        spade_conditional_input_dims = opt.semantic_nc
        if not opt.no_3dnoise:
            spade_conditional_input_dims += opt.z_dim

        self.norm_0 = norms.IWT_SPADE_HWT(opt, fin, spade_conditional_input_dims)
        self.norm_1 = norms.IWT_SPADE_HWT(opt, fmiddle, spade_conditional_input_dims)
        if self.learned_shortcut:
            self.norm_s = norms.IWT_SPADE_HWT(opt, fin, spade_conditional_input_dims)
        self.activ = nn.LeakyReLU(0.2, inplace=True)

    def forward(self, x, seg, edges = None):


        if self.learned_shortcut:
            x_s = self.conv_s(x)
            #x_s = self.conv_s(self.norm_s(x, seg))
        else:
            x_s = x

        dx = self.conv_0(self.activ(self.norm_0(x, seg)))
        dx = self.conv_1(self.activ(self.norm_1(dx, seg)))
        return x_s,dx


class WaveletBlock_with_SPADE_residual_too(nn.Module):
    def __init__(self, fin, fout, opt):
        super().__init__()
        self.opt = opt
        self.learned_shortcut = (fin != fout)
        fmiddle = min(fin, fout)
        sp_norm = norms.get_spectral_norm(opt)
        self.conv_0 = sp_norm(nn.Conv2d(fin, fmiddle, kernel_size=3, padding=1))
        self.conv_1 = sp_norm(nn.Conv2d(fmiddle, fout, kernel_size=3, padding=1))
        if self.learned_shortcut:
            self.conv_s = sp_norm(nn.Conv2d(fin, fout, kernel_size=1, bias=False))

        spade_conditional_input_dims = opt.semantic_nc
        if not opt.no_3dnoise:
            spade_conditional_input_dims += opt.z_dim

        self.norm_0 = norms.SPADE(opt, fin, spade_conditional_input_dims)
        self.norm_1 = norms.SPADE(opt, fmiddle, spade_conditional_input_dims)
        if self.learned_shortcut:
            self.norm_s = norms.SPADE(opt, fin, spade_conditional_input_dims)
        self.activ = nn.LeakyReLU(0.2, inplace=True)

    def forward(self, x, seg, edges = None):


        if self.learned_shortcut:
            #x_s = self.conv_s(x)
            x_s = self.conv_s(self.norm_s(x, seg))
        else:
            x_s = x

        dx = self.conv_0(self.activ(self.norm_0(x, seg)))
        dx = self.conv_1(self.activ(self.norm_1(dx, seg)))
        return x_s,dx


class ResidualWaveletGenerator_2(nn.Module):
    def __init__(self, opt):
        super().__init__()
        self.opt = opt
        sp_norm = norms.get_spectral_norm(opt)
        ch = opt.channels_G
        self.channels = [4*16*ch, 4*16*ch, 4*16*ch, 4*8*ch, 4*4*ch, 4*2*ch, 4*1*ch]

        self.init_W, self.init_H = self.compute_latent_vector_size(opt)

        self.conv_img = ToRGB_wavelet(in_channel=self.channels[-1],upsample = False)
        self.iwt = InverseHaarTransform(3)

        self.up = nn.Upsample(scale_factor=2)
        self.up_residual = IWT_Upsample_HWT(factor=2,mode='bilinear')
        self.body = nn.ModuleList([])
        for i in range(len(self.channels)-1):
            self.body.append(WaveletBlock_with_SPADE(self.channels[i], self.channels[i+1], opt))

        if not self.opt.no_3dnoise:
            self.fc = nn.Conv2d(self.opt.semantic_nc + self.opt.z_dim, 4*16 * ch, 3, padding=1)
        else:
            self.fc = nn.Conv2d(self.opt.semantic_nc, 4*16 * ch, 3, padding=1)

#        self.constant_input = ConstantInput(self.channels[0],(self.init_W, self.init_H))

    def compute_latent_vector_size(self, opt):
        w = opt.crop_size // (2**(opt.num_res_blocks-1))
        h = round(w / opt.aspect_ratio)
        return h//2, w//2

    def forward(self, input, z=None,edges = None):
        seg = input
        if self.opt.gpu_ids != "-1":
            seg.cuda()
        if not self.opt.no_3dnoise:
            dev = seg.get_device() if self.opt.gpu_ids != "-1" else "cpu"
            z = torch.randn(seg.size(0), self.opt.z_dim, dtype=torch.float32, device=dev)
            z = z.view(z.size(0), self.opt.z_dim, 1, 1)
            z = z.expand(z.size(0), self.opt.z_dim, seg.size(2), seg.size(3))
            seg = torch.cat((z, seg), dim = 1)


        x = F.interpolate(seg, size=(self.init_W, self.init_H))

        x = self.fc(x)
        for i in range(self.opt.num_res_blocks):
            x_s,x = self.body[i](x, seg,edges)

            if i < self.opt.num_res_blocks-1:
                x = self.up(x)
                x_s = self.up_residual(x_s)
                x = x+x_s


        x = self.conv_img(x+x_s)
        x = self.iwt(x)

        return x

