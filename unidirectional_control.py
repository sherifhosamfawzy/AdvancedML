# -*- coding: utf-8 -*-
"""unidirectional-control.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1FD-80WX4I2dsoyuj_T4QXTs-6Tfpvf6d
"""

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torchtext.datasets import TranslationDataset, Multi30k, IWSLT, WMT14
from torchtext.data import Field, BucketIterator, Iterator
import spacy
import numpy as np
import random
import math
import time
from collections import defaultdict
from matplotlib import pyplot as plt
from nltk.translate.bleu_score import sentence_bleu, SmoothingFunction
import itertools

# Commented out IPython magic to ensure Python compatibility.
# %%capture
# !python3 -m spacy download en
# !python3 -m spacy download de
# 
# spacy_de = spacy.load('de')
# spacy_en = spacy.load('en')

from google.colab import drive
drive.mount('/content/drive')

sos_token='<sos>'
eos_token='<eos>'
pad_token='<pad>'
unk_token='<unk>'

def tokenize_de(text):
    return [token.text for token in spacy_de.tokenizer(text)]

def tokenize_en(text):
    return [token.text for token in spacy_en.tokenizer(text)]

TRG = Field(init_token=sos_token, 
            eos_token=eos_token,
            lower=True, 
            tokenize=tokenize_en,
            pad_token=pad_token, 
            unk_token=unk_token)

SRC = Field(init_token=sos_token, 
            eos_token=eos_token,
            lower=True, 
            tokenize=tokenize_de,
            pad_token=pad_token, 
            unk_token=unk_token)

# Commented out IPython magic to ensure Python compatibility.
# %%capture
# train_data, valid_data, test_data = IWSLT.splits(exts = ('.de', '.en'), 
#                         fields = (SRC, TRG),
#                         filter_pred=lambda x: len(x.__dict__['src']) <= 50)

SRC.build_vocab(train_data, max_size=10000)
TRG.build_vocab(train_data, max_size=10000)

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

BATCH_SIZE = 80

train_iterator, valid_iterator, test_iterator = BucketIterator.splits(
    (train_data, valid_data, test_data), 
    sort_key=lambda x: len(x.__dict__['src']),
    batch_size = BATCH_SIZE, 
    device = device)

class Encoder(nn.Module):
    def __init__(self, input_dim, emb_dim, hid_dim):
        super().__init__()
        self.hid_dim = hid_dim
        self.embedding = nn.Embedding(input_dim, emb_dim)
        self.rnn = nn.GRU(emb_dim, hid_dim)
        
    def forward(self, src):
        embedded = self.embedding(src)
        outputs, hidden = self.rnn(embedded)
        return hidden

class Decoder(nn.Module):
    def __init__(self, output_dim, emb_dim, hid_dim):
        super().__init__()
        self.hid_dim = hid_dim
        self.output_dim = output_dim
        self.embedding = nn.Embedding(output_dim, emb_dim)
        self.rnn = nn.GRU(emb_dim + hid_dim, hid_dim)
        self.fc_out = nn.Linear(emb_dim + (hid_dim * 2), output_dim)
        
    def forward(self, input, hidden, context):
        input = input.unsqueeze(0)
        embedded = self.embedding(input)
        emb_con = torch.cat((embedded, context), dim = 2)
        output, hidden = self.rnn(emb_con, hidden)
        output = torch.cat((embedded.squeeze(0), hidden.squeeze(0), context.squeeze(0)), 
                           dim = 1)
        prediction = self.fc_out(output)
        return prediction, hidden

class EncoderDecoder(nn.Module):
    def __init__(self, encoder, decoder, device):
        super().__init__()
        
        self.encoder = encoder
        self.decoder = decoder
        self.device = device

    def forward(self, src, trg, teacher_forcing_ratio = 0.5):
        batch_size = trg.shape[1]
        trg_len = trg.shape[0]
        trg_vocab_size = self.decoder.output_dim
        outputs = torch.zeros(trg_len, batch_size, trg_vocab_size).to(self.device)
        context = self.encoder(src)
        hidden = context
        input = trg[0,:]
        
        for t in range(1, trg_len):
            output, hidden = self.decoder(input, hidden, context)
            outputs[t] = output
            teacher_force = random.random() < teacher_forcing_ratio
            top1 = output.argmax(1)
            input = trg[t] if teacher_force else top1

        return outputs
        
    def translate(self, sentence, max_len=50, beam_width=3):
        self.eval()
        src = SRC.process([sentence]).to(device)
        context = self.encoder(src)
        hidden = context
        beams = [(0.0, [TRG.vocab.stoi[TRG.init_token]], hidden)]
        done = False
        log_smax = nn.LogSoftmax(dim=0).to(device)

        while not done:
            for i in range(len(beams)):
                p, sent, hidden = beams.pop(0)
                if len(sent) >= max_len or sent[-1] == TRG.vocab.stoi[TRG.eos_token]:
                    beams += [(p, sent, hidden)]
                    continue
                trg = torch.ones(1, dtype=torch.int64).to(device) * sent[-1]
                pred, hidden = self.decoder(trg, hidden, context)
                ll = log_smax(pred[0])
                top_ll, top_t = torch.topk(ll, k=beam_width)
                beams += [(p + ll, sent + [t], hidden) for ll, t in zip(top_ll, top_t)]
            beams = sorted(beams, reverse=True)[:beam_width]
            done = all([b[1][-1] == TRG.vocab.stoi[TRG.eos_token] or len(b[1]) >= max_len for b in beams])

        trgs = beams.pop(0)[1]
        return [TRG.vocab.itos[i] for i in trgs]

INPUT_DIM = len(SRC.vocab)
OUTPUT_DIM = len(TRG.vocab)
ENC_EMB_DIM = 300
DEC_EMB_DIM = 300
HID_DIM = 600

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

enc = Encoder(INPUT_DIM, ENC_EMB_DIM, HID_DIM)
dec = Decoder(OUTPUT_DIM, DEC_EMB_DIM, HID_DIM)
enc_dec = EncoderDecoder(enc, dec, device).to(device)

def init_weights(m):
    for name, param in m.named_parameters():
        if 'bias' in name:
            nn.init.zeros_(param.data)
        elif 'rnn.weight' in name:
            nn.init.orthogonal_(param.data)
        else:
            nn.init.normal_(param.data, mean=0, std=0.01)

enc_dec.apply(init_weights)
ed_optimizer = optim.Adam(enc_dec.parameters())
TRG_PAD_IDX = TRG.vocab.stoi[TRG.pad_token]
criterion = nn.CrossEntropyLoss(ignore_index = TRG_PAD_IDX)

def train(model, iterator, optimizer, criterion, clip):
    
    model.train()
    
    epoch_loss = 0
    
    for i, batch in enumerate(iterator):
        src = batch.src
        trg = batch.trg
        
        optimizer.zero_grad()
        
        output = model(src, trg)
        
        #trg = [trg len, batch size]
        #output = [trg len, batch size, output dim]
        
        output_dim = output.shape[-1]
        
        output = output[1:].view(-1, output_dim)
        trg = trg[1:].view(-1)
        
        #trg = [(trg len - 1) * batch size]
        #output = [(trg len - 1) * batch size, output dim]
        
        loss = criterion(output, trg)
        
        loss.backward()
        
        torch.nn.utils.clip_grad_norm_(model.parameters(), clip)
        
        optimizer.step()
        
        epoch_loss += loss.item()
        
    return epoch_loss / len(iterator)

def epoch_time(start_time, end_time):
    elapsed_time = end_time - start_time
    elapsed_mins = int(elapsed_time / 60)
    elapsed_secs = int(elapsed_time - (elapsed_mins * 60))
    return elapsed_mins, elapsed_secs

N_EPOCHS = 10
CLIP = 1

for epoch in range(N_EPOCHS):
    torch.save(enc_dec.state_dict(), f'/content/drive/My Drive/ml-mini-project/unidirectional-control/epoch-{epoch}.pt')
    enc_dec.load_state_dict(torch.load(f'/content/drive/My Drive/ml-mini-project/unidirectional-control/epoch-{epoch}.pt'))

    start_time = time.time()
    train_loss = train(enc_dec, train_iterator, ed_optimizer, criterion, CLIP)
    end_time = time.time()
    
    epoch_mins, epoch_secs = epoch_time(start_time, end_time)

    print(f'Epoch: {epoch+1:02} | Time: {epoch_mins}m {epoch_secs}s')
    print(f'\tTrain Loss: {train_loss:.3f}')

torch.save(enc_dec.state_dict(), f'/content/drive/My Drive/ml-mini-project/unidirectional-control/epoch-{N_EPOCHS}.pt')
enc_dec.load_state_dict(torch.load(f'/content/drive/My Drive/ml-mini-project/unidirectional-control/epoch-{N_EPOCHS}.pt'))

def evaluate_bleu(model, datasets):
    overall_size = sum([len(d) for d in datasets])
    total = 0.0
    for i, example in enumerate(itertools.chain(*datasets)):
        pred = model.translate(example.src)[1:-2]
        total += sentence_bleu([example.trg], pred, smoothing_function=SmoothingFunction().method1)
        if i % 200 == 199: print('.', end='')
    print('')
    return total / overall_size

valid_bleu = [0.0]
for i in range(1, 11):
    enc_dec.load_state_dict(torch.load(f'/content/drive/My Drive/models/control/non-attention-model-6.pt'))
    valid_bleu.append(evaluate_bleu(enc_dec, [valid_data]))