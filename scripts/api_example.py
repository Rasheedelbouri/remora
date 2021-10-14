from remora.data_chunks import RemoraRead
from remora.model_util import load_onnx_model
from remora.inference import call_read_mods

read = RemoraRead.test_read()
model, model_metadata = load_onnx_model("remora_train_results/model_final.onnx")
preds, labels, pos = call_read_mods(read, model, model_metadata)