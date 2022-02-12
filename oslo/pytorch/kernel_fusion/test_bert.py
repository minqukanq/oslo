import time

from transformers import BertTokenizer
from transformers.models.bert.modeling_bert import BertForMaskedLM as ModelClass

from oslo.pytorch.kernel_fusion.manage.manager import FusionManager

model = ModelClass.from_pretrained("bert-base-cased").cuda()
tokenizer = BertTokenizer.from_pretrained("bert-base-cased")

fusion = FusionManager(model, "fuser2")
fusion.register()

start = time.time()
model(**tokenizer("hello I am Kevin.", return_tensors="pt").to("cuda"))
print(time.time() - start)

start = time.time()
model(**tokenizer("hello I am Kevin. Hi", return_tensors="pt").to("cuda"))
print(time.time() - start)

start = time.time()
model(**tokenizer("hello I am Kevin. Hi Hello Bye", return_tensors="pt").to("cuda"))
print(time.time() - start)

start = time.time()
model(**tokenizer("hello I am Kevin. Hi Hello Bye", return_tensors="pt").to("cuda"))
print(time.time() - start)

start = time.time()
model(**tokenizer("hello I am Kevin. Hi Hello Bye", return_tensors="pt").to("cuda"))
print(time.time() - start)


start = time.time()
model(**tokenizer("hello I am Kevin. Hi Hello Bye", return_tensors="pt").to("cuda"))
print(time.time() - start)


start = time.time()
model(**tokenizer("hello I am Kevin. Hi Hello Bye", return_tensors="pt").to("cuda"))
print(time.time() - start)
