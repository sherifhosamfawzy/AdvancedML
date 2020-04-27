# -*- coding: utf-8 -*-
"""hidden-experiment.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1AwBs1NadpAnkJ7blmWghCzbeWlGHxd3r
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
# train_data, _, _ = IWSLT.splits(exts = ('.de', '.en'), 
#                         fields = (SRC, TRG),
#                         filter_pred=lambda x: len(x.__dict__['src']) <= 50)
# 
# test_10, valid_10, valid_12 = IWSLT.splits(exts = ('.de', '.en'), 
#                                 fields = (SRC, TRG),
#                                 train='IWSLT16.TED.tst2010',
#                                 test='IWSLT16.TED.dev2010',
#                                 validation='IWSLT16.TEDX.dev2012',
#                                 filter_pred=lambda x: len(x.__dict__['src']) <= 60)
# 
# test_11, test_12, test_13 = IWSLT.splits(exts = ('.de', '.en'), 
#                         fields = (SRC, TRG),
#                         train='IWSLT16.TED.tst2011',
#                         validation='IWSLT16.TED.tst2012',
#                         test='IWSLT16.TED.tst2013',
#                         filter_pred=lambda x: len(x.__dict__['src']) <= 60)
# 
# test_13x, test_14x, test_14 = IWSLT.splits(exts = ('.de', '.en'), 
#                         fields = (SRC, TRG),
#                         train='IWSLT16.TEDX.tst2013',
#                         validation='IWSLT16.TEDX.tst2014',
#                         test='IWSLT16.TED.tst2014',
#                         filter_pred=lambda x: len(x.__dict__['src']) <= 60)
# 
# valid_data = [valid_10, valid_12]
# test_data = [test_10, test_11, test_12, test_13, test_13x, test_14x, test_14]

SRC.build_vocab(train_data, max_size=10000)
TRG.build_vocab(train_data, max_size=10000)

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

class EncoderAttnBi(nn.Module):
    def __init__(self, input_dim, emb_dim, enc_hid_dim, dec_hid_dim):
        super().__init__()
        self.embedding = nn.Embedding(input_dim, emb_dim)
        self.rnn = nn.GRU(emb_dim, enc_hid_dim, bidirectional = True)
        
    def forward(self, src):
        embedded = self.embedding(src)
        outputs, hidden = self.rnn(embedded)
        return outputs, hidden[1]

class AttentionBi(nn.Module):
    def __init__(self, enc_hid_dim, dec_hid_dim):
        super().__init__()
        
        self.attn = nn.Linear(2 * enc_hid_dim + dec_hid_dim, dec_hid_dim)
        self.v = nn.Linear(dec_hid_dim, 1, bias = False)
        
    def forward(self, hidden, encoder_outputs):
        batch_size = encoder_outputs.shape[1]
        src_len = encoder_outputs.shape[0]
        
        hidden = hidden.unsqueeze(1).repeat(1, src_len, 1)
        encoder_outputs = encoder_outputs.permute(1, 0, 2)
        
        energy = torch.tanh(self.attn(torch.cat((hidden, encoder_outputs), dim = 2))) 
        attention = self.v(energy).squeeze(2)
        
        return F.softmax(attention, dim=1)

class DecoderAttnBi(nn.Module):
    def __init__(self, output_dim, emb_dim, enc_hid_dim, dec_hid_dim, attention):
        super().__init__()
        self.output_dim = output_dim
        self.attention = attention
        self.embedding = nn.Embedding(output_dim, emb_dim)
        self.rnn = nn.GRU((enc_hid_dim * 2) + emb_dim, dec_hid_dim)
        self.fc_out = nn.Linear((enc_hid_dim * 2) + dec_hid_dim + emb_dim, output_dim)
        
    def forward(self, input, hidden, encoder_outputs):
        input = input.unsqueeze(0)
        embedded = self.embedding(input)
        a = self.attention(hidden, encoder_outputs)
        a = a.unsqueeze(1)
        encoder_outputs = encoder_outputs.permute(1, 0, 2)
        weighted = torch.bmm(a, encoder_outputs)
        weighted = weighted.permute(1, 0, 2)        
        rnn_input = torch.cat((embedded, weighted), dim = 2)
        output, hidden = self.rnn(rnn_input, hidden.unsqueeze(0))
        embedded = embedded.squeeze(0)
        output = output.squeeze(0)
        weighted = weighted.squeeze(0)
        
        prediction = self.fc_out(torch.cat((output, weighted, embedded), dim = 1))
        return prediction, hidden.squeeze(0)

class SearchBi(nn.Module):
    def __init__(self, encoder, decoder, device):
        super().__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.device = device
        
    def forward(self, src, trg, teacher_forcing_ratio = 0.5):
        batch_size = src.shape[1]
        trg_len = trg.shape[0]
        trg_vocab_size = self.decoder.output_dim
        
        outputs = torch.zeros(trg_len, batch_size, trg_vocab_size).to(self.device)
        encoder_outputs, hidden = self.encoder(src)
        input = trg[0,:]
        
        for t in range(1, trg_len):
            output, hidden = self.decoder(input, hidden, encoder_outputs)
            outputs[t] = output
            teacher_force = random.random() < teacher_forcing_ratio
            top1 = output.argmax(1)
            input = trg[t] if teacher_force else top1

        return outputs

        
    def translate(self, sentence, max_len=50, beam_width=3):
        self.eval()
        src = SRC.process([sentence]).to(device)
        encoder_out, hidden = self.encoder(src)
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
                pred, hidden = self.decoder(trg, hidden, encoder_out)
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
HID_DIM = [200, 400, 600, 800, 1000]
MAX_LENGTH = 50

att = [AttentionBi(h, h) for h in HID_DIM]
enc = [EncoderAttnBi(INPUT_DIM, ENC_EMB_DIM, h, h)for h in HID_DIM]
dec = [DecoderAttnBi(OUTPUT_DIM, DEC_EMB_DIM, h, h, a) for h, a in zip(HID_DIM, att)]
search_bi = [SearchBi(e, d, device).to(device) for e, d in zip(enc, dec)]

def evaluate_bleu(model, datasets, ignore_unk=False):
    total = 0.0
    count = 0.0
    scores = np.zeros(sum([len(d) for d in datasets]))
    for i, example in enumerate(itertools.chain(*datasets)):
        sent = SRC.process([example.src])
        if ignore_unk and SRC.vocab.stoi[SRC.unk_token] in sent:
            continue
        pred = model.translate(example.src)[1:-2]
        score = sentence_bleu([example.trg], pred, smoothing_function=SmoothingFunction().method1)
        scores[i] = score
        if i % 1000 == 999: print('.', end='')
    print('')
    return scores.mean()
    
def bleu_summary(model, datasets, ignore_unk=False):
    overall_examples = sum([len(d) for d in datasets])
    lengths = np.zeros(overall_examples, dtype=np.int32)
    scores = np.zeros(overall_examples)

    for i, example in enumerate(itertools.chain(*datasets)):
        sent = SRC.process([example.src])
        if ignore_unk and SRC.vocab.stoi[SRC.unk_token] in sent:
            continue
        pred = model.translate(example.src)
        scores[i] = sentence_bleu([example.trg], pred, smoothing_function=SmoothingFunction().method1)
        lengths[i] = len(example.src)
        if i % 900 == 899: print('.', end='')
      
    scores = scores[lengths != 0.0]    
    lengths = lengths[lengths != 0.0]

    print('')
    means, up, lo = {}, {}, {}
    uniq_lengths, counts = np.unique(lengths, return_counts=True)
    for l, c in zip(uniq_lengths, counts):
        s = scores[lengths == l]
        means[l] = s.mean()
        up[l] = means[l] + (np.sqrt(s.var() / c) * 1.65)
        lo[l] = means[l] - (np.sqrt(s.var() / c) * 1.65)

    uniq_lengths = [l for l in sorted(uniq_lengths)]
    return uniq_lengths, [means[l] for l in uniq_lengths], [up[l] for l in uniq_lengths], [lo[l] for l in uniq_lengths]

def get_lengths(datasets):
    overall_examples = sum([len(d) for d in datasets])
    lengths = np.zeros(overall_examples, dtype=np.int32)
    for i, example in enumerate(itertools.chain(*datasets)):
        lengths[i] = len(example.src)
    return lengths

TRG_PAD_IDX = TRG.vocab.stoi[TRG.pad_token]
criterion = nn.CrossEntropyLoss(ignore_index = TRG_PAD_IDX)

BATCH_SIZE = 80

train_iterator, valid_iterator, test_iterator = BucketIterator.splits(
    (train_data, valid_12, test_13), 
    sort_key=lambda x: len(x.__dict__['src']),
    batch_size = BATCH_SIZE, 
    device = device)

def evaluate(model, iterator, criterion):
    model.eval()
    epoch_loss = 0
    with torch.no_grad():
        for i, batch in enumerate(iterator):
            src = batch.src
            trg = batch.trg
            output = model(src, trg, 0) 
            output_dim = output.shape[-1]
            output = output[1:].view(-1, output_dim)
            trg = trg[1:].view(-1)
            loss = criterion(output, trg)
            epoch_loss += loss.item()
        
    return epoch_loss / len(iterator)

valid_score = np.zeros((len(HID_DIM), 6))
train_err = np.zeros((len(HID_DIM), 6))


for i in range(len(HID_DIM)):
    for epoch in range(6):
        search_bi[i].load_state_dict(torch.load(f'/content/drive/My Drive/ml-mini-project/hidden/attention-model-{HID_DIM[i]}-{epoch}.pt'))
        valid_score[i][epoch] = evaluate_bleu(search_bi[i], valid_data)
        train_err[i][epoch] = evaluate(search_bi[i], train_iterator, criterion)

valid_score

train_err

search_bi[0].load_state_dict(torch.load(f'/content/drive/My Drive/ml-mini-project/hidden/attention-model-200-5.pt'))
search_bi[1].load_state_dict(torch.load(f'/content/drive/My Drive/ml-mini-project/hidden/attention-model-400-5.pt'))
search_bi[2].load_state_dict(torch.load(f'/content/drive/My Drive/ml-mini-project/hidden/attention-model-600-5.pt'))
search_bi[3].load_state_dict(torch.load(f'/content/drive/My Drive/ml-mini-project/hidden/attention-model-800-4.pt'))
search_bi[4].load_state_dict(torch.load(f'/content/drive/My Drive/ml-mini-project/hidden/attention-model-1000-3.pt'))

res = []
ls = get_lengths(test_data)
for s in search_bi:
    res.append(evaluate_bleu(s, test_data))

def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)

n = 9663

# res = [0.16942321591401932, 0.17180345054113969, 0.1702285792543723, 0.1708189162399337, 0.1655442693664769]
counts = [16280004, 24986404, 35132804, 46719204, 59745604]
mean = [d.mean() for d in res]
var_ = [d.var() for d in res]
diff = [(1.65 * np.sqrt(v / n)) for m, v in zip(mean, var_)]

fig, ax = plt.subplots(figsize=(10,8))
ax.set_axisbelow(True)
ax.minorticks_on()
ax.grid(which='major', linestyle='-', linewidth='0.5', color='black')
ax.grid(which='minor', linestyle=':', linewidth='0.5', color='black')
ax.tick_params(which='both', top='off', left='off', right='off', bottom='off')


labels = ['200', '400', '600', '800', '1000']
ax.bar(labels, mean, yerr=diff, color='grey', alpha=0.7, capsize=10)
ax.set_xlabel('Hidden Units')
ax.set_ylabel('Average BLEU Score')
ax.set_title('BLEU Score Performance by Number of Hidden Units')
ax.set_ybound(16.0, 17.5)

plt.show()

fig, ax = plt.subplots(figsize=(10,8))
ax.set_axisbelow(True)
ax.minorticks_on()
ax.grid(which='major', linestyle='-', linewidth='0.5', color='black')
ax.grid(which='minor', linestyle=':', linewidth='0.5', color='black')
ax.tick_params(which='both', top='off', left='off', right='off', bottom='off')

params = lambda x: (18 * (x ** 2)) + (32732 * x) + 9013604
x = [i for i in range(0, 1050, 50)]
ax.plot(x, [params(i) for i in x], color='black', marker='x', markevery=4)
ax.set_xbound(0, 1000)
ax.set_ylabel('Trainable Parameters')
ax.set_xlabel('Hidden Units')
ax.set_title('Number of Trainable Parameters by Hidden Units in Encoder and Decoder')

plt.show()