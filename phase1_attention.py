import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torchtext.datasets import TranslationDataset
from torchtext.data import Field, BucketIterator
import spacy
import numpy as np
import random
import math
import time
from collections import defaultdict
from matplotlib import pyplot as plt
from nltk.translate.bleu_score import sentence_bleu, SmoothingFunction
from google.colab import drive

# Set the seed so that results are reproducible
SEED = 1234
random.seed(SEED)
np.random.seed(SEED)
torch.manual_seed(SEED)
torch.cuda.manual_seed(SEED)
torch.backends.cudnn.deterministic = True

# Mount Google Drive to access data sets
drive.mount('/content/gdrive')

# Define Tokens
sos_token='<sos>'
eos_token='<eos>'
pad_token='<pad>'
unk_token='<unk>'

# Tokenization function
def tokenize(text):
    return text.split()

# Create Fields for both source and target languages
# using the tokenization function defined and lowercasing the text
sourceLanguage = targetLanguage = Field(sequential = True,
                                        use_vocab = True,
                                        init_token = sos_token,
                                        eos_token = eos_token,
                                        fix_length = None,
                                        dtype = torch.long,
                                        lower = True,
                                        tokenize = tokenize,
                                        pad_token = pad_token,
                                        unk_token = unk_token)

# Load data from Google Drive, use only training data with maximum lenght of 50
train_data = TranslationDataset("/content/gdrive/My Drive/data/europarl-v7.fr-en",
                             exts=('.en', '.fr'),
                             fields=(sourceLanguage, targetLanguage),
                             filter_pred=lambda x: len(x.__dict__['src']) <= 50)

valid_data = TranslationDataset("/content/gdrive/My Drive/data/newstest2013",
                             exts=('.en', '.fr'),
                             fields=(sourceLanguage, targetLanguage))

test_data = TranslationDataset("/content/gdrive/My Drive/data/newstest2014-fren-src",
                             exts=('.en.sgm', '.fr.sgm'),
                             fields=(sourceLanguage, targetLanguage))

# Build vocabulary for source and target languages using words
# with at least 100 occurences in dataset and limit vocab size to 30000
sourceLanguage.build_vocab(train_data, min_freq = 100, max_size = 30000)
targetLanguage.build_vocab(train_data, min_freq = 100, max_size = 30000)
# Use the cuda device if available
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

BATCH_SIZE = 80
INPUT_DIM  = len(sourceLanguage.vocab) # Kx = Size of Source Vocabulary
OUTPUT_DIM = len(targetLanguage.vocab) # Ky = Size of Target Vocabulary
EMB_DIM = 620 # m = Dimension of Embedding
HID_DIM = 1000 # n = Dimension of Hidden Units
MAXOUT_DIM = 500 # l = Dimension of Maxout Hidden Layer
ATT_HID_DIM = 1000 # n' = Number of Hidden Units in alignment model
MAX_LENGTH = 50 # Maximum length of sentence used

# Iterators that iterate through train, validation and test data
train_iterator = Iterator(
    train_data,
    batch_size = BATCH_SIZE,
    sort_key = lambda x: len(x.src),
    device = device
)

valid_iterator = Iterator(
    valid_data,
    batch_size = BATCH_SIZE,
    sort_key = lambda x: len(x.src),
    device = device
)

test_iterator = Iterator(
    test_data,
    batch_size = BATCH_SIZE,
    sort_key = lambda x: len(x.src),
    device = device
)

# Encoder Layer
# Input:
#   1) source: source sentence
# Outputs:
#   1) encoder_outputs: the hidden states of the source sentence
#   2) hidden: the input to the first GRU of the decoder
class Encoder(nn.Module):
    def __init__(self, input_dimension, embedding_dimension, hidden_dimension):
        super().__init__()
        self.embedding = nn.Embedding(input_dimension, embedding_dimension)
        self.rnn = nn.GRU(embedding_dimension, hidden_dimension, bidirectional = True)
        self.fc = nn.Linear(hidden_dimension * 2, hidden_dimension)
        # Monodirectional Implementation:
        # self.rnn = nn.GRU(embedding_dimension, hidden_dimension, bidirectional = False)
        # self.fc = nn.Linear(hidden_dimension, hidden_dimension)

    def forward(self, source):
        # Embedding Layer
        embedded = self.embedding(source)
        # Bidirectional GRU-based RNN
        encoder_outputs, hidden = self.rnn(embedded)
        # Hidden layer pased as input to decoder
        hidden = torch.tanh(self.fc(hidden[1,:,:]))
        # Monodirectional Implementation:
        # hidden = torch.tanh(self.fc(hidden[0,:,:]))
        return encoder_outputs, hidden

# Attention Layer
# Inputs:
#   1) hidden: previous hidden state of decoder
#   2) encoder_outputs: hidden states from the encoder
# Ouput:
#   1) Weights alpha_i_j
class Attention(nn.Module):
    def __init__(self, hidden_dimension, attention_hidden_dimension):
        super().__init__()
        self.attn = nn.Linear((hidden_dimension * 2) + hidden_dimension, attention_hidden_dimension)
        # Monodirectional Implementation:
        # self.attn = nn.Linear(hidden_dimension + hidden_dimension, attention_hidden_dimension)
        self.v = nn.Linear(attention_hidden_dimension, 1, bias = False)

    def forward(self, hidden, encoder_outputs):
        batch_size = encoder_outputs.shape[1]
        source_length = encoder_outputs.shape[0]
        # Repeat decoder hidden state source_length times
        # This is done to calculate the energy between the hidden state
        # and each of the encoder's source_length hidden states
        hidden = hidden.unsqueeze(1).repeat(1, source_length, 1)
        encoder_outputs = encoder_outputs.permute(1, 0, 2)
        # Calculate alignment model (also known as energy)
        energy = torch.tanh(self.attn(torch.cat((hidden, encoder_outputs), dim = 2)))
        # Single perceptron
        attention = self.v(energy).squeeze(2)
        # Return softmax of alignment model
        return F.softmax(attention, dim=1)

# Decoder Layer
# Inputs:
#   1) input: y_t-1 used to compute y_t
#   2) hidden: s_t-1 used to in layer t
#   3) encoder_outputs: hidden states from the encoder
# Outputs:
#   1) prediction: y_t
#   2) hidden: s_t
class Decoder(nn.Module):
    def __init__(self, output_dimension, embedding_dimension, hidden_dimension, attention, maxout_dimension):
        super().__init__()
        self.output_dimension = output_dimension
        self.maxout_dimension = maxout_dimension
        self.attention = attention
        self.embedding = nn.Embedding(output_dimension, embedding_dimension)
        self.rnn = nn.GRU((hidden_dimension * 2) + embedding_dimension , hidden_dimension)
        self.maxout = nn.Linear((hidden_dimension * 2) + hidden_dimension + embedding_dimension, 2 * maxout_dimension)
        # Monodirectional Implementation:
        # self.rnn = nn.GRU((hidden_dimension) + embedding_dimension , hidden_dimension)
        # self.maxout = nn.Linear((hidden_dimension) + hidden_dimension + embedding_dimension, 2 * maxout_dimension)
        self.fc_out = nn.Linear(maxout_dimension, output_dimension)

    def forward(self, input, hidden, encoder_outputs):
        # Reshape Input
        input = input.unsqueeze(0)
        # Embedding Layer
        embedded = self.embedding(input)
        # Attention Layer
        a = self.attention(hidden, encoder_outputs)
        # Reshape attention output
        a = a.unsqueeze(1)

        # Reshape encoder_outputs
        encoder_outputs = encoder_outputs.permute(1, 0, 2)
        # Calculate context using encoder_outputs and attention output
        context = torch.bmm(a, encoder_outputs)
        # Reshape context
        context = context.permute(1, 0, 2)
        # Concatenate context and y_t-1
        rnn_input = torch.cat((embedded, context), dim = 2)

        # GRU-based RNN
        output, hidden = self.rnn(rnn_input, hidden.unsqueeze(0))

        assert (output == hidden).all()
        # Since output == hidden, use output. Can't use hidden because of a shaping issue
        # Essentially, they are the same tensor but one is [1,1,x] and the other is [1,x]

        # Reshape tensors
        embedded = embedded.squeeze(0)
        output = output.squeeze(0)
        context = context.squeeze(0)

        # Maxout layer
        t_init = self.maxout(torch.cat((output, context, embedded), dim = 1)) # Size 2xl
        batch_size = t_init.shape[0]
        t_init = t_init.view(batch_size ,self.maxout_dimension, 2)
        t, _ = torch.max(t_init,2)
        t = t.view(batch_size,t.shape[1])  # Size l
        # FC layer
        prediction = self.fc_out(t)
        # Return prediciton y_t along with hidden state s_t
        return prediction, hidden.squeeze(0)

# Model Encapsulating all Layers
# Inputs:
#   1) src: source sentence
#   2) trg: target sentence
#   3) train: bool indicating if model currenlty used for training or evaluation
# Outputs:
#   1) outputs_train/outputs_evaluate: All the y_ts predicted by the model
class Seq2SeqBiDirectionalSearch(nn.Module):
    def __init__(self, encoder, decoder, max_length, device):
        super().__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.device = device
        self.max_length = max_length

    def forward(self, src, trg, train):
        batch_size = src.shape[1]
        target_length = trg.shape[0]
        target_vocab_size = self.decoder.output_dimension
        # Output tensors used to hold predictions, if train = 1 then outputs_train is used
        # with size target_length, however when evaluating (i.e. train = 0), outputs_evaluate
        # is used with size max_length
        outputs_train = torch.zeros(target_length, batch_size, target_vocab_size).to(self.device)
        outputs_evaluate = torch.zeros(self.max_length, batch_size, target_vocab_size).to(self.device)
        # Call encoder layer and get encoder_outputs and hidden
        encoder_outputs, hidden = self.encoder(src)

        input = trg[0,:] # SOS (Start-Of-Sentence) used as the first input y_1

        # If in training phase, run decoder target_length times, with output
        # hidden used as s_t-1 to next stage t and decoder_output used as y_t-1
        if train == 1: # training
          for t in range(1, target_length):
              decoder_output, hidden = self.decoder(input, hidden, encoder_outputs)
              outputs_train[t] = decoder_output
              input = decoder_output.argmax(1)
          # Return decoder predictions
          return outputs_train
        # If in evaluation phase, run decoder till EOS predicted or till max_length
        # of sentence
        elif train == 0: # evaluate
          # Evaluate source sentences individually, i.e. batching not used
          for batch_idx in range(batch_size):
            input_temp = input[batch_idx].reshape(1)
            hidden_temp = hidden[batch_idx, :].reshape(1, hidden.shape[1])
            encoder_outputs_temp = encoder_outputs[:, batch_idx, :].reshape(encoder_outputs.shape[0], 1, encoder_outputs.shape[2])
            for t in range(1, self.max_length):
                decoder_output, hidden_temp = self.decoder(input_temp, hidden_temp , encoder_outputs_temp )
                outputs_evaluate[t, batch_idx]= decoder_output
                input_temp = decoder_output.argmax(1)
                # If y_t = EOS return
                if targetLanguage.vocab.itos[input_temp] == targetLanguage.eos_token:
                  break
          # Return decoder predictions
          return outputs_evaluate

# Instantiate layers and model
enc = Encoder(INPUT_DIM, EMB_DIM, HID_DIM)
attn = Attention(HID_DIM,ATT_HID_DIM)
dec = Decoder(OUTPUT_DIM, EMB_DIM, HID_DIM, attn, MAXOUT_DIM)
model = Seq2SeqBiDirectionalSearch(enc, dec, MAX_LENGTH, device).to(device)

# Initialize weights
def init_weights(m):
    for name, param in m.named_parameters():
        if 'gru.weight_hh' in name:
            nn.init.orthogonal_(param.data)
        elif 'attn.weight' in name:
            nn.init.normal_(param.data, mean=0, std=0.001)
        elif 'v.weight' in name or 'bias' in name:
            nn.init.constant_(param.data, 0)
        else:
            nn.init.normal_(param.data, mean=0, std=0.01)

model.apply(init_weights)

# Create Adadelta optimizer
optimizer = optim.Adadelta(model.parameters(), rho=0.95, eps=1e-06)
# Initialize cross entropy loss
TRG_PAD_IDX = targetLanguage.vocab.stoi[targetLanguage.pad_token]
criterion = nn.CrossEntropyLoss(ignore_index = TRG_PAD_IDX)

# Train the model and compute training loss
def train(model, iterator, optimizer, criterion, clip):

    model.train()
    epoch_loss = 0

    # Loop through epoch
    for i, batch in enumerate(iterator):
      src = batch.src
      trg = batch.trg
      optimizer.zero_grad()

      # Model with train = 1
      output = model(src, trg, 1)
      # Reshape Output and Target
      output_dim = output.shape[-1]
      output = output[1:].view(-1, output_dim)
      trg = trg[1:].view(-1)

      # Compute loss
      loss = criterion(output, trg)
      # Backpropagation
      loss.backward()
      # Use gradient clipping
      torch.nn.utils.clip_grad_norm_(model.parameters(), clip)
      optimizer.step()
      epoch_loss += loss.item()

    # Return average loss
    return epoch_loss / len(iterator)

# Convert one-hot vectors to text
def one_hot_to_text(one_hot, languageModel, filter_unk=False):
  return [languageModel.vocab.itos[idx] for idx in one_hot if not (filter_unk and idx == TRG_PAD_IDX )]

# Train the model and compute blue score
def evaluate(model, iterator):

    model.eval()
    # Store Bleu score per sentence length
    counts = defaultdict(float)
    scores = defaultdict(float)
    epoch_bleu_score = 0

    with torch.no_grad():
        # Loop through epoch
        for i, batch in enumerate(iterator):
            src = batch.src
            trg = batch.trg

            # Model with train = 1
            output = model(src, trg, 0)
            sentence_size, batch_size, vocab_size = output.shape

            # Compute Bleu Score for sentences in current batch
            for batch_idx in range(batch_size):
                probs = F.softmax(output[:,batch_idx,:], 1)
                _, sentence_by_idx = probs.max(axis=1)
                score = sentence_bleu([one_hot_to_text(trg[:,batch_idx], targetLanguage)], one_hot_to_text(sentence_by_idx, targetLanguage), smoothing_function=SmoothingFunction().method1)
                length = len([1 for word in src[:,batch_idx] if word != targetLanguage.vocab.stoi[targetLanguage.pad_token]])
                counts[length] += 1
                scores[length] += score
                epoch_bleu_score += score
    # Return bleu score average for each sentence length + total bleu score for epoch
    return {length: scores[length] / counts[length] for length in scores}, epoch_bleu_score

# Calculate time spent by a single epoch
def epoch_time(start_time, end_time):
    elapsed_time = end_time - start_time
    elapsed_mins = int(elapsed_time / 60)
    elapsed_secs = int(elapsed_time - (elapsed_mins * 60))
    return elapsed_mins, elapsed_secs

# Number of Epochs
N_EPOCHS = 1
# Gradient Clipping
CLIP = 1

best_valid_loss = float('inf')
training_loss_array = []

for epoch in range(N_EPOCHS):

    start_time = time.time()
    # Train the model using training set
    train_loss = train(model, train_iterator, optimizer, criterion, CLIP)
    # Compute bleu scores using validation set
    bleu_scores, epoch_bleu_score = evaluate(model, valid_iterator)
    end_time = time.time()
    epoch_mins, epoch_secs = epoch_time(start_time, end_time)

    training_loss_array.append(train_loss)

    print(f'Epoch: {epoch+1:02} | Time: {epoch_mins}m {epoch_secs}s')
    print(f'\tTrain Loss: {train_loss: }')
    print(f'\tEpoch Blue Score: ', epoch_bleu_score)
    print(f'\tBlue Scores: ', bleu_scores)
    # Checkpoint to ensure progress isn't lost
    torch.save(model.state_dict(), f'/content/gdrive/My Drive/data/attention-model-bidirectional-{epoch}.pt')

# Plot Training Loss
plt.title("Training Loss")
plt.xlabel("Number of Epochs")
plt.ylabel("Training Loss")
plt.plot(range(len(training_loss_array)), training_loss_array)
plt.show()

# Load and evaluate models on test data
model.load_state_dict(torch.load(f'/content/gdrive/My Drive/control_and_attention_model/attention-model-bidirectional-1.pt'))
bleu_scores, epoch_bleu_score = evaluate(model, test_iterator)

# Plot Bleu Scores as a function of sentence length
plt.title("Bleu Scores")
plt.xlabel("Sentence Length")
plt.ylabel("Bleu Score")
lists = sorted(bleu_scores.items())
x, y = zip(*lists)
plt.plot(x, y)
plt.show()
plt.savefig('bleu_scores.png')
