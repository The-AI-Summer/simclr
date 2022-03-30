# -*- coding: utf-8 -*-
"""AI Summer SimCLR Resnet18 STL10.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1LhKx8FPwn8GvLbiaOp3m2wZOPZE44tgZ

# SimCLR in STL10 with Resnet18 AI Summer tutorial

## Imports, basic utils, augmentations and Contrastive loss
"""

!pip install pytorch-lightning
!pip install lightning-bolts
import torch
import torchvision.models as models
import numpy as np
import os
import torch
import torchvision.transforms as T
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision.datasets import STL10
from torch.utils.data import DataLoader
from torch.multiprocessing import cpu_count
import torchvision.transforms as T

def default(val, def_val):
    return def_val if val is None else val

def reproducibility(config):
    SEED = int(config.seed)
    torch.manual_seed(SEED)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(SEED)
    if (config.cuda):
        torch.cuda.manual_seed(SEED)


def device_as(t1, t2):
    """
    Moves t1 to the device of t2
    """
    return t1.to(t2.device)

# From https://github.com/PyTorchLightning/pytorch-lightning/issues/924
def weights_update(model, checkpoint_path):
    checkpoint = torch.load(checkpoint_path)
    model_dict = model.state_dict()
    pretrained_dict = {k: v for k, v in checkpoint['state_dict'].items() if k in model_dict}
    model_dict.update(pretrained_dict)
    model.load_state_dict(model_dict)
    print(f'Checkpoint {checkpoint_path} was loaded')
    return model


class Augment:
    """
    A stochastic data augmentation module
    Transforms any given data example randomly
    resulting in two correlated views of the same example,
    denoted x ̃i and x ̃j, which we consider as a positive pair.
    """

    def __init__(self, img_size, s=1):
        color_jitter = T.ColorJitter(
            0.8 * s, 0.8 * s, 0.8 * s, 0.2 * s
        )
        # 10% of the image
        blur = T.GaussianBlur((3, 3), (0.1, 2.0))

        self.train_transform = T.Compose(
            [
            T.RandomResizedCrop(size=img_size),
            T.RandomHorizontalFlip(p=0.5),  # with 0.5 probability
            T.RandomApply([color_jitter], p=0.8),
            T.RandomApply([blur], p=0.5),
            T.RandomGrayscale(p=0.2),
            # imagenet stats
            T.ToTensor(),
            T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
            ]
        )

        self.test_transform = T.Compose(
            [
                T.ToTensor(),
                T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
            ]
        )

    def __call__(self, x):
        return self.train_transform(x), self.train_transform(x)



def get_stl_dataloader(batch_size, transform=None, split="unlabeled"):

    stl10 = STL10("./", split=split, transform=transform, download=True)
    return DataLoader(dataset=stl10, batch_size=batch_size, num_workers=cpu_count()//2)

import matplotlib.pyplot as plt

def imshow(img):
    """
    shows an imagenet-normalized image on the screen
    """
    mean = torch.tensor([0.485, 0.456, 0.406], dtype=torch.float32)
    std = torch.tensor([0.229, 0.224, 0.225], dtype=torch.float32)
    unnormalize = T.Normalize((-mean / std).tolist(), (1.0 / std).tolist())
    npimg = unnormalize(img).numpy()
    plt.imshow(np.transpose(npimg, (1, 2, 0)))
    plt.show()



class ContrastiveLoss(nn.Module):
    """
    Vanilla Contrastive loss, also called InfoNceLoss as in SimCLR paper
    """
    def __init__(self, batch_size, temperature=0.5):
        super().__init__()
        self.batch_size = batch_size
        self.temperature = temperature
        self.mask = (~torch.eye(batch_size * 2, batch_size * 2, dtype=bool)).float()

    def calc_similarity_batch(self, a, b):
        representations = torch.cat([a, b], dim=0)
        return F.cosine_similarity(representations.unsqueeze(1), representations.unsqueeze(0), dim=2)

    def forward(self, proj_1, proj_2):
        """
        proj_1 and proj_2 are batched embeddings [batch, embedding_dim]
        where corresponding indices are pairs
        z_i, z_j in the SimCLR paper
        """
        batch_size = proj_1.shape[0]
        z_i = F.normalize(proj_1, p=2, dim=1)
        z_j = F.normalize(proj_2, p=2, dim=1)

        similarity_matrix = self.calc_similarity_batch(z_i, z_j)

        sim_ij = torch.diag(similarity_matrix, batch_size)
        sim_ji = torch.diag(similarity_matrix, -batch_size)

        positives = torch.cat([sim_ij, sim_ji], dim=0)

        nominator = torch.exp(positives / self.temperature)

        denominator = device_as(self.mask, similarity_matrix) * torch.exp(similarity_matrix / self.temperature)

        all_losses = -torch.log(nominator / torch.sum(denominator, dim=1))
        loss = torch.sum(all_losses) / (2 * self.batch_size)
        return loss

dataset = STL10("./", split='train', transform=Augment(96), download=True)
imshow(dataset[99][0][0])
imshow(dataset[99][0][0])
imshow(dataset[99][0][0])
imshow(dataset[99][0][0])

"""## Add projection Head for embedding and training logic with pytorch lightning model"""

import pytorch_lightning as pl
import torch
import torch.nn.functional as F
from pl_bolts.optimizers.lr_scheduler import LinearWarmupCosineAnnealingLR
from torch.optim import SGD, Adam


class AddProjection(nn.Module):
    def __init__(self, config, model=None, mlp_dim=512):
        super(AddProjection, self).__init__()
        embedding_size = config.embedding_size
        self.backbone = default(model, models.resnet18(pretrained=False, num_classes=config.embedding_size))
        mlp_dim = default(mlp_dim, self.backbone.fc.in_features)
        print('Dim MLP input:',mlp_dim)
        self.backbone.fc = nn.Identity()

        # add mlp projection head
        self.projection = nn.Sequential(
            nn.Linear(in_features=mlp_dim, out_features=mlp_dim),
            nn.BatchNorm1d(mlp_dim),
            nn.ReLU(),
            nn.Linear(in_features=mlp_dim, out_features=embedding_size),
            nn.BatchNorm1d(embedding_size),
        )

    def forward(self, x, return_embedding=False):
        embedding = self.backbone(x)
        if return_embedding:
            return embedding
        return self.projection(embedding)



def define_param_groups(model, weight_decay, optimizer_name):
    def exclude_from_wd_and_adaptation(name):
        if 'bn' in name:
            return True
        if optimizer_name == 'lars' and 'bias' in name:
            return True

    param_groups = [
        {
            'params': [p for name, p in model.named_parameters() if not exclude_from_wd_and_adaptation(name)],
            'weight_decay': weight_decay,
            'layer_adaptation': True,
        },
        {
            'params': [p for name, p in model.named_parameters() if exclude_from_wd_and_adaptation(name)],
            'weight_decay': 0.,
            'layer_adaptation': False,
        },
    ]
    return param_groups


class SimCLR_pl(pl.LightningModule):
    def __init__(self, config, model=None, feat_dim=512):
        super().__init__()
        self.config = config
        
        self.model = AddProjection(config, model=model, mlp_dim=feat_dim)

        self.loss = ContrastiveLoss(config.batch_size, temperature=self.config.temperature)

    def forward(self, X):
        return self.model(X)

    def training_step(self, batch, batch_idx):
        (x1, x2), labels = batch
        z1 = self.model(x1)
        z2 = self.model(x2)
        loss = self.loss(z1, z2)
        self.log('Contrastive loss', loss, on_step=True, on_epoch=True, prog_bar=True, logger=True)
        return loss

    def configure_optimizers(self):
        max_epochs = int(self.config.epochs)
        param_groups = define_param_groups(self.model, self.config.weight_decay, 'adam')
        lr = self.config.lr
        optimizer = Adam(param_groups, lr=lr, weight_decay=self.config.weight_decay)

        print(f'Optimizer Adam, '
              f'Learning Rate {lr}, '
              f'Effective batch size {self.config.batch_size * self.config.gradient_accumulation_steps}')

        scheduler_warmup = LinearWarmupCosineAnnealingLR(optimizer, warmup_epochs=10, max_epochs=max_epochs,
                                                         warmup_start_lr=0.0)

        return [optimizer], [scheduler_warmup]

"""## Hyperparameters, and configuration stuff"""

# a lazy way to pass the config file
class Hparams:
    def __init__(self):
        self.epochs = 300 # number of training epochs
        self.seed = 77777 # randomness seed
        self.cuda = True # use nvidia gpu
        self.img_size = 96 #image shape
        self.save = "./saved_models/" # save checkpoint
        self.load = False # load pretrained checkpoint
        self.gradient_accumulation_steps = 5 # gradient accumulation steps
        self.batch_size = 200
        self.lr = 3e-4 # for ADAm only
        self.weight_decay = 1e-6
        self.embedding_size= 128 # papers value is 128
        self.temperature = 0.5 # 0.1 or 0.5
        self.checkpoint_path = './SimCLR_ResNet18.ckpt' # replace checkpoint path here

"""## Pretraining main logic"""

import torch
from pytorch_lightning import Trainer
import os
from pytorch_lightning.callbacks import GradientAccumulationScheduler
from pytorch_lightning.callbacks import ModelCheckpoint
from torchvision.models import  resnet18

available_gpus = len([torch.cuda.device(i) for i in range(torch.cuda.device_count())])
save_model_path = os.path.join(os.getcwd(), "saved_models/")
print('available_gpus:',available_gpus)
filename='SimCLR_ResNet18_adam_'
resume_from_checkpoint = False
train_config = Hparams()

reproducibility(train_config)
save_name = filename + '.ckpt'

model = SimCLR_pl(train_config, model=resnet18(pretrained=False), feat_dim=512)

transform = Augment(train_config.img_size)
data_loader = get_stl_dataloader(train_config.batch_size, transform)

accumulator = GradientAccumulationScheduler(scheduling={0: train_config.gradient_accumulation_steps})
checkpoint_callback = ModelCheckpoint(filename=filename, dirpath=save_model_path,every_n_val_epochs=2,
                                        save_last=True, save_top_k=2,monitor='Contrastive loss_epoch',mode='min')

if resume_from_checkpoint:
  trainer = Trainer(callbacks=[accumulator, checkpoint_callback],
                  gpus=available_gpus,
                  max_epochs=train_config.epochs,
                  resume_from_checkpoint=train_config.checkpoint_path)
else:
  trainer = Trainer(callbacks=[accumulator, checkpoint_callback],
                  gpus=available_gpus,
                  max_epochs=train_config.epochs)


trainer.fit(model, data_loader)


trainer.save_checkpoint(save_name)
from google.colab import files
files.download(save_name)

"""## Save only backbone weights from Resnet18 that are only necessary for fine tuning"""

model_pl = SimCLR_pl(train_config, model=resnet18(pretrained=False))
model_pl = weights_update(model_pl, "SimCLR_ResNet18_adam_.ckpt")

resnet18_backbone_weights = model_pl.model.backbone
print(resnet18_backbone_weights)
torch.save({
            'model_state_dict': resnet18_backbone_weights.state_dict(),
            }, 'resnet18_backbone_weights.ckpt')

"""# Fine-tuning from SSL simclr checkpoint"""

import pytorch_lightning as pl
import torch
from torch.optim import SGD


class SimCLR_eval(pl.LightningModule):
    def __init__(self, lr, model=None, linear_eval=False):
        super().__init__()
        self.lr = lr
        self.linear_eval = linear_eval
        if self.linear_eval:
          model.eval()
        self.mlp = torch.nn.Sequential(
            torch.nn.Linear(512,10),
            # torch.nn.ReLU(),
            # torch.nn.Dropout(0.1),
            # torch.nn.Linear(128, 10)
        )

        self.model = torch.nn.Sequential(
            model, self.mlp
        )
        self.loss = torch.nn.CrossEntropyLoss()

    def forward(self, X):
        return self.model(X)

    def training_step(self, batch, batch_idx):
        x, y = batch
        z = self.forward(x)
        loss = self.loss(z, y)
        self.log('Cross Entropy loss', loss, on_step=True, on_epoch=True, prog_bar=True, logger=True)

        predicted = z.argmax(1)
        acc = (predicted == y).sum().item() / y.size(0)
        self.log('Train Acc', acc, on_step=False, on_epoch=True, prog_bar=True, logger=True)

        return loss
    
    def validation_step(self, batch, batch_idx):
        x, y = batch
        z = self.forward(x)
        loss = self.loss(z, y)
        self.log('Val CE loss', loss, on_step=True, on_epoch=True, prog_bar=False, logger=True)

        predicted = z.argmax(1)
        acc = (predicted == y).sum().item() / y.size(0)
        self.log('Val Accuracy', acc, on_step=True, on_epoch=True, prog_bar=True, logger=True)

        return loss

    def configure_optimizers(self):
        if self.linear_eval:
          print(f"\n\n Attention! Linear evaluation \n")
          optimizer = SGD(self.mlp.parameters(), lr=self.lr, momentum=0.9)
        else:
          optimizer = SGD(self.model.parameters(), lr=self.lr, momentum=0.9)
        return [optimizer]


class Hparams:
    def __init__(self):
        self.epochs = 100 # number of training epochs
        self.seed = 77777 # randomness seed
        self.cuda = True # use nvidia gpu
        self.img_size = 96 #image shape
        self.save = "./saved_models/" # save checkpoint
        self.gradient_accumulation_steps = 1 # gradient accumulation steps
        self.batch_size = 128
        self.lr = 1e-3 
        self.embedding_size= 128 # papers value is 128
        self.temperature = 0.5 # 0.1 or 0.5

import torch
from pytorch_lightning import Trainer
from pytorch_lightning.callbacks import GradientAccumulationScheduler
import os
from pytorch_lightning.callbacks import ModelCheckpoint
from torchvision.models import resnet18


# general stuff
available_gpus = len([torch.cuda.device(i) for i in range(torch.cuda.device_count())])
train_config = Hparams()
save_model_path = os.path.join(os.getcwd(), "saved_models/")
print('available_gpus:', available_gpus)
filename = 'SimCLR_ResNet18_finetune_'
reproducibility(train_config)
save_name = filename + '_Final.ckpt'

# load resnet backbone
backbone = models.resnet18(pretrained=False)
backbone.fc = nn.Identity()
checkpoint = torch.load('resnet18_backbone_weights.ckpt')
backbone.load_state_dict(checkpoint['model_state_dict'])
model = SimCLR_eval(train_config.lr, model=backbone, linear_eval=False)

# preprocessing and data loaders
transform_preprocess = Augment(train_config.img_size).test_transform
data_loader = get_stl_dataloader(train_config.batch_size, transform=transform_preprocess,split='train')
data_loader_test = get_stl_dataloader(train_config.batch_size, transform=transform_preprocess,split='test')


# callbacks and trainer
accumulator = GradientAccumulationScheduler(scheduling={0: train_config.gradient_accumulation_steps})

checkpoint_callback = ModelCheckpoint(filename=filename, dirpath=save_model_path,save_last=True,save_top_k=2,
                                       monitor='Val Accuracy_epoch', mode='max')

trainer = Trainer(callbacks=[checkpoint_callback,accumulator],
                  gpus=available_gpus,
                  max_epochs=train_config.epochs)

trainer.fit(model, data_loader,data_loader_test)
trainer.save_checkpoint(save_name)

"""# Finetune from Imageget pretraining"""

# load model
resnet = models.resnet18(pretrained=False)
resnet.fc = nn.Identity()
print('imagenet weights, no pretraining')
model = SimCLR_eval(train_config.lr, model=resnet, linear_eval=False)

# preprocessing and data loaders
transform_preprocess = Augment(train_config.img_size).test_transform
data_loader = get_stl_dataloader(128, transform=transform_preprocess,split='train')
data_loader_test = get_stl_dataloader(128, transform=transform_preprocess,split='test')

checkpoint_callback = ModelCheckpoint(filename=filename, dirpath=save_model_path)

trainer = Trainer(callbacks=[checkpoint_callback],
                  gpus=available_gpus,
                  max_epochs=train_config.epochs)

trainer.fit(model, data_loader, data_loader_test)
trainer.save_checkpoint(save_name)

# Commented out IPython magic to ensure Python compatibility.
# %load_ext tensorboard
# %tensorboard --logdir ./lightning_logs/ --port 6010