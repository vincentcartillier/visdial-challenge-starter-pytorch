import argparse
import datetime
import gc
import math
import os
import time

import torch
from torch import nn, optim
from torch.autograd import Variable
from torch.optim import lr_scheduler
from torch.utils.data import DataLoader

from dataloader import VisDialDataset
from encoders.lf import LateFusionEncoder
from decoders.disc import DiscriminativeDecoder


parser = argparse.ArgumentParser()
VisDialDataset.add_cmdline_args(parser)
LateFusionEncoder.add_cmdline_args(parser)

parser.add_argument_group('Optimization related arguments')
parser.add_argument('-num_epochs', default=20, type=int, help='Epochs')
parser.add_argument('-batch_size', default=16, type=int, help='Batch size')
parser.add_argument('-lr', default=1e-3, type=float, help='Learning rate')
parser.add_argument('-lr_decay_rate', default=0.9997592083, type=float, help='Decay for lr')
parser.add_argument('-min_lr', default=5e-5, type=float, help='Minimum learning rate')
parser.add_argument('-weight_init', default='xavier', choices=['xavier', 'kaiming'],
                        help='Weight initialization strategy')
parser.add_argument('-gpuid', default=0, type=int, help='GPU id to use')

parser.add_argument_group('Checkpointing related arguments')
parser.add_argument('-load_path', default='', help='Checkpoint to load path from')
parser.add_argument('-save_path', default='checkpoints/', help='Path to save checkpoints')
parser.add_argument('-save_step', default=2, type=int,
                        help='Save checkpoint after every save_step epochs')

# ----------------------------------------------------------------------------
# input arguments and options
# ----------------------------------------------------------------------------

args = parser.parse_args()
start_time = datetime.datetime.strftime(datetime.datetime.utcnow(), '%d-%b-%Y-%H:%M:%S')
if args.save_path == 'checkpoints/':
    args.save_path += start_time

# seed for reproducibility
torch.manual_seed(1234)

# set device and default tensor type
if args.gpuid >= 0:
    torch.cuda.manual_seed_all(1234)
    torch.cuda.set_device(args.gpuid)

# transfer all options to model
model_args = args

# ----------------------------------------------------------------------------
# read saved model and args
# ----------------------------------------------------------------------------

if args.load_path != '':
    components = torch.load(args.load_path)
    model_args = components['encoder'].args
    model_args.gpuid = args.gpuid
    model_args.batch_size = args.batch_size

    # lr is saved separately as it changes during training
    args.lr = components['lr']
    # this is required by dataloader
    args.img_norm = components['encoder'].args.img_norm

# set this because only late fusion encoder is supported yet
args.concat_history = True

for arg in vars(args):
    print('{:<20}: {}'.format(arg, getattr(args, arg)))

# ----------------------------------------------------------------------------
# loading dataset wrapping with a dataloader
# ----------------------------------------------------------------------------

dataset = VisDialDataset(args, ['train'])
dataloader = DataLoader(dataset,
                        batch_size=args.batch_size,
                        shuffle=True,
                        collate_fn=dataset.collate_fn)

# ----------------------------------------------------------------------------
# setting model args
# ----------------------------------------------------------------------------

# transfer some useful args from dataloader to model
for key in {'num_data_points', 'vocab_size', 'max_ques_count',
            'max_ques_len', 'max_ans_len'}:
    setattr(model_args, key, getattr(dataset, key))

model_args.training = True

# iterations per epoch
setattr(args, 'iter_per_epoch', 
    math.ceil(dataset.num_data_points['train'] / args.batch_size))
print("{} iter per epoch.".format(args.iter_per_epoch))

# ----------------------------------------------------------------------------
# setup the model
# ----------------------------------------------------------------------------

if args.load_path != '':
    encoder = components['encoder']
    decoder = components['decoder']
    print("Loaded model from {}".format(args.load_path))
else:
    print("Encoder: {}".format('lf-ques-im-hist'))  # todo: support more encoders
    print("Decoder: {}".format('disc'))

    encoder = LateFusionEncoder(model_args)
    decoder = DiscriminativeDecoder(model_args)

criterion = nn.CrossEntropyLoss()
optimizer = optim.Adam(list(encoder.parameters()) + list(decoder.parameters()),
                       lr=args.lr)
scheduler = lr_scheduler.StepLR(optimizer, step_size=1, gamma=args.lr_decay_rate)

if args.gpuid >= 0:
    encoder = encoder.cuda()
    decoder = decoder.cuda()
    criterion = criterion.cuda()

# ----------------------------------------------------------------------------
# training
# ----------------------------------------------------------------------------

print("Training start time: {}".format(
    datetime.datetime.strftime(datetime.datetime.utcnow(), '%d-%b-%Y-%H:%M:%S')))
encoder.train()
decoder.train()
os.makedirs(args.save_path, exist_ok=True)

running_loss = 0.0
train_begin = time.time()
for epoch in range(1, model_args.num_epochs + 1):
    for i, batch in enumerate(dataloader):
        optimizer.zero_grad()

        if args.gpuid >= 0:
            for key in batch:
                batch[key] = Variable(batch[key].cuda())
        
        enc_out = encoder(batch['img_feat'], batch['ques_fwd'], batch['hist'])
        dec_out = decoder(enc_out, batch['opt'])
        cur_loss = criterion(dec_out, batch['ans_ind'].view(-1))
        cur_loss.backward()

        optimizer.step()
        gc.collect()

        # --------------------------------------------------------------------
        # update running loss and decay learning rates
        # --------------------------------------------------------------------

        if running_loss > 0.0:
            running_loss = 0.95 * running_loss + 0.05 * cur_loss.data[0]
        else:
            running_loss = cur_loss.data[0]

        if optimizer.param_groups[0]['lr'] > args.min_lr:
            scheduler.step()

        # --------------------------------------------------------------------
        # print after ever few iterations
        # --------------------------------------------------------------------

        if i % 100 == 0:
            # print current time, running average, learning rate, iteration, epoch
            print("[{}][Epoch: {:3d}][Iter: {:6d}][Loss: {:6f}][lr: {:7f}]".format(
                datetime.timedelta(int(time.time() - train_begin)), epoch,
                    epoch * args.iter_per_epoch + i, running_loss,
                    optimizer.param_groups[0]['lr']))

    # ------------------------------------------------------------------------
    # save checkpoints and final model
    # ------------------------------------------------------------------------
    if epoch % args.save_step == 0:
        torch.save({
            'encoder': encoder,
            'decoder': decoder,
            'lr': optimizer.param_groups[0]['lr']
        }, os.path.join(args.save_path, 'model_epoch_{}.pth'.format(epoch)))

torch.save({'encoder': encoder, 'decoder': decoder},
    os.path.join(args.save_path, 'model_final.pth'))