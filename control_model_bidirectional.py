# -*- coding: utf-8 -*-
"""control-model-bidirectional.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/15PZ8XfDYUvKby15CEfuyhGfnpledtpk7
"""

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torchtext.datasets import TranslationDataset, IWSLT
from torchtext.data import Field, Iterator, Dataset
import spacy
import numpy as np
import random
import math
import time
from collections import defaultdict
from nltk.translate.bleu_score import sentence_bleu

# Random seeds defined for the sake of reproducibility
SEED = 1234
random.seed(SEED)
np.random.seed(SEED)
torch.manual_seed(SEED)
torch.cuda.manual_seed(SEED)
torch.backends.cudnn.deterministic = True

"""## Data Loading
Loads data from google drive and builds the vocabulary used in the one hot vector.
"""

from google.colab import drive
drive.mount('/content/gdrive')

# Commented out IPython magic to ensure Python compatibility.
# %%capture
# sos_token='<sos>'
# eos_token='<eos>'
# pad_token='<pad>'
# unk_token='<unk>'
# 
# def tokenize(text):
#     return text.split()
# 
# # while the paper does not lower case all the words, we do in order to minimize 
# # the size of the vocabulary due to more limited resources.
# sourceLanguage = targetLanguage = Field(sequential=True, 
#                                         use_vocab=True, 
#                                         init_token=sos_token, 
#                                         eos_token=eos_token, 
#                                         fix_length=None, 
#                                         dtype=torch.long, 
#                                         lower=True, 
#                                         tokenize=tokenize,
#                                         pad_token=pad_token, 
#                                         unk_token=unk_token)

# Manually filtering out all sentences that are longer than 50 as is done in
# the proposed training set in the paper
dataset = TranslationDataset("/content/gdrive/My Drive/data/europarl-v7.fr-en", 
                             exts=('.en', '.fr'), 
                             fields=(sourceLanguage, targetLanguage),
                             filter_pred=lambda x: len(x.__dict__['src']) <= 50)

# Using a minimum frequency here is another method used to reduce the size of 
# the model while ensuring the most frequent words are accounted for.
sourceLanguage.build_vocab(dataset, min_freq = 100)
targetLanguage.build_vocab(dataset, min_freq = 100)

"""## Constants
Defines the dimensions used for the model.
"""

# Usefull constant for moving tensors onto the appropriate device
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

BATCH_SIZE  = 80
INPUT_DIM   = len(sourceLanguage.vocab)
OUTPUT_DIM  = len(targetLanguage.vocab)
EMB_DIM     = 256
HID_DIM     = 512
MAXOUT_DIM  = 400
MAX_LENGTH  = 50

train_iterator = Iterator(
    dataset, 
    batch_size = BATCH_SIZE,
    sort_key = lambda x: len(x.src), 
    device = device
)

"""## Model Definition
Defines the encoder, decoder, and the ensemble sequence to sequence translation model.
"""

class EncoderRNNEncDecBiDirectional(nn.Module):
    def __init__(self, input_dim, emb_dim, hid_dim):
        super(EncoderRNNEncDecBiDirectional, self).__init__()
        self.embedding  = nn.Embedding(input_dim, emb_dim)
        self.rnn        = nn.GRU(emb_dim, hid_dim, bidirectional=True)
        self.fc_out     = nn.Linear(hid_dim, hid_dim) # fully connected out layer
        self.activation = nn.Tanh() # activation for the final layer
        
    def forward(self, input):
        output          = self.embedding(input)
        output, hidden  = self.rnn(output)
        output          = self.activation(self.fc_out(hidden[1,:,:]))
        output          = output.unsqueeze(0)
        return output

class DecoderRNNEncDecBiDirectional(nn.Module):
    def __init__(self, output_dim, emb_dim, hid_dim, max_dim):
        super(DecoderRNNEncDecBiDirectional, self).__init__()
        self.embedding  = nn.Embedding(output_dim, emb_dim)
        self.rnn        = nn.GRU(emb_dim + hid_dim, hid_dim)
        self.max_dim    = max_dim
        self.max_out    = nn.Linear(emb_dim + hid_dim * 2, 2 * max_dim)
        self.out        = nn.Linear(max_dim, output_dim)

    def forward(self, input, hidden, context):
        embedded        = self.embedding(input)                     
        output          = torch.cat((embedded, context), dim = 2)
        _, hidden       = self.rnn(output, hidden)
        output          = torch.cat((embedded.squeeze(0), 
                                     hidden.squeeze(0), 
                                     context.squeeze(0)), 
                                    dim = 1)
        output          = self.max_out(output)
        output          = output.view(input.shape[1], self.max_dim, 2)
        output, _       = torch.max(output, 2) # acitvation for maxout layer
        output          = self.out(output)
        return output, hidden

class Seq2SeqEncDecBiDirectional(nn.Module):
    def __init__(self, input_dim, emb_dim, hid_dim, output_dim, max_dim):
        super(Seq2SeqEncDecBiDirectional, self).__init__() 
        self.fc_in      = nn.Linear(hid_dim, hid_dim) # fully connected layer for context
        self.fc_act     = nn.Tanh() # activation for context
        self.encoder    = EncoderRNNEncDecBiDirectional(input_dim, emb_dim, hid_dim)
        self.decoder    = DecoderRNNEncDecBiDirectional(output_dim, emb_dim, hid_dim, max_dim)
        self.output_dim = output_dim
    
    def forward(self, src, trg, is_train=False):
        context         = self.encoder(src)
        decoder_hidden  = self.fc_in(context.squeeze(0))
        decoder_hidden  = self.fc_act(decoder_hidden).unsqueeze(0)
        outputs         = torch.zeros(trg.shape[0], 
                                      trg.shape[1], 
                                      self.output_dim).to(device)
        input           = trg[0]
        for t in range(1, trg.shape[0]):
            decoder_output, decoder_hidden = self.decoder(input.unsqueeze(0), 
                                                          decoder_hidden, 
                                                          context)
            outputs[t] = decoder_output

            # if in training use the actual values else use predicted values
            input = trg[t] if is_train else decoder_output.argmax(1)
        return outputs

model       = Seq2SeqEncDecBiDirectional(INPUT_DIM, EMB_DIM, HID_DIM, OUTPUT_DIM, MAXOUT_DIM).to(device)
optimizer   = optim.Adadelta(model.parameters(), rho=0.95, eps=1e-06)
TRG_PAD_IDX = targetLanguage.vocab.stoi[targetLanguage.pad_token]

# Ignore differences on padding since these aren't indicative of error
criterion   = nn.CrossEntropyLoss(ignore_index = TRG_PAD_IDX)

"""## Training Utils
Utility methods to be used during the training phase.
"""

def train(model, iterator, optimizer, criterion, max_length=MAX_LENGTH):
    model.train()
    epoch_loss = 0.0
    for i, batch in enumerate(iterator):
        # ignore sentences that are too large
        if batch.src.shape[0] > max_length: continue
        optimizer.zero_grad()
        src, trg    = batch.src.to(device), batch.trg.to(device)
        outputs     = model(src, trg, is_train=True)
        output_dim  = outputs.shape[-1]
        outputs     = outputs[1:].view(-1, output_dim)
        trg         = trg[1:].view(-1)
        loss        = criterion(outputs, trg)
        loss.backward()
        optimizer.step()
        epoch_loss += loss.item()
    return epoch_loss / len(iterator) # average loss

def epoch_time(start_time, end_time):
    elapsed_time = end_time - start_time
    elapsed_mins = int(elapsed_time / 60)
    elapsed_secs = int(elapsed_time - (elapsed_mins * 60))
    return elapsed_mins, elapsed_secs

"""## Training

Defines the training procedure and checkpoint model saving to ensure no loss of progress in the instance of a timeout from colab.
"""

N_EPOCHS = 5

for epoch in range(N_EPOCHS):
    train_iterator.init_epoch() # Processes like shuffling that happen before epoch.
    start_time = time.time()
    train_loss = train(model, train_iterator, optimizer, criterion) 
    end_time = time.time()
    epoch_mins, epoch_secs = epoch_time(start_time, end_time)
    print(f'Epoch: {epoch+1:02} | Time: {epoch_mins}m {epoch_secs}s')
    print(f'\tTrain Loss: {train_loss: }')

    # Checkpoint to ensure progrerss isn't lost.
    torch.save(model.state_dict(), f'/content/gdrive/My Drive/models/control-model-bidirectional-{epoch}.pt')
    model.load_state_dict(torch.load(f'/content/gdrive/My Drive/models/control-model-bidirectional-{epoch}.pt'))
    model.eval()

