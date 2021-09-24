import atexit
import os
from shutil import copyfile
import warnings

import numpy as np
import torch
from tqdm import tqdm

from remora import util, log
from remora.data_chunks import load_datasets

LOGGER = log.get_logger()

# filter warnings about GPU on environments without a GPU
warnings.filterwarnings(action="ignore", category=UserWarning, module="torch")


def train_model(
    seed,
    device,
    out_path,
    dataset_path,
    num_chunks,
    mod_motif,
    focus_offset,
    chunk_context,
    val_prop,
    batch_size,
    num_data_workers,
    model_path,
    size,
    optimizer,
    lr,
    weight_decay,
    lr_decay_step,
    lr_decay_gamma,
    base_pred,
    epochs,
    save_freq,
    kmer_context_bases,
):
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
        torch.cuda.set_device(device)
    elif device is not None:
        LOGGER.warning(
            "Device option specified, but CUDA is not available from torch."
        )

    val_fp = util.ValidationLogger(out_path, base_pred)
    atexit.register(val_fp.close)
    batch_fp = util.BatchLogger(out_path)
    atexit.register(batch_fp.close)

    LOGGER.info("Loading model")
    copyfile(model_path, os.path.join(out_path, "model.py"))
    num_out = 4 if base_pred else 2
    model = util._load_python_model(
        model_path,
        size=size,
        kmer_len=sum(kmer_context_bases) + 1,
        num_out=num_out,
    )
    LOGGER.info(f"Model structure:\n{model}")

    LOGGER.info("Preparing training settings")
    criterion = torch.nn.CrossEntropyLoss()
    if torch.cuda.is_available():
        model = model.cuda()
        criterion = criterion.cuda()
    if optimizer == "sgd":
        opt = torch.optim.SGD(
            model.parameters(),
            lr=float(lr),
            weight_decay=weight_decay,
            momentum=0.9,
            nesterov=True,
        )
    elif optimizer == "adam":
        opt = torch.optim.Adam(
            model.parameters(),
            lr=float(lr),
            weight_decay=weight_decay,
        )
    else:
        opt = torch.optim.AdamW(
            model.parameters(),
            lr=float(lr),
            weight_decay=weight_decay,
        )
    training_var = {
        "epoch": 0,
        "model_path": model_path,
        "state_dict": {},
    }
    start_epoch = training_var["epoch"]
    state_dict = training_var["state_dict"]
    if state_dict != {}:
        model.load_state_dict(state_dict)
    scheduler = torch.optim.lr_scheduler.StepLR(
        opt, step_size=lr_decay_step, gamma=lr_decay_gamma
    )

    LOGGER.info("Loading training dataset")
    dl_trn, dl_val, dl_val_trn = load_datasets(
        dataset_path,
        chunk_context,
        focus_offset,
        batch_size,
        num_chunks,
        fixed_seq_len_chunks=model._variable_width_possible,
        mod_motif=mod_motif,
        base_pred=base_pred,
        val_prop=val_prop,
        num_data_workers=num_data_workers,
        kmer_context_bases=kmer_context_bases,
    )

    LOGGER.info("Running initial validation")
    # assess accuracy before first iteration
    val_acc, val_loss = val_fp.validate_model(model, criterion, dl_val, 0)
    trn_acc, trn_loss = val_fp.validate_model(
        model, criterion, dl_val_trn, 0, "trn"
    )

    LOGGER.info("Start training")
    ebar = tqdm(
        total=epochs,
        smoothing=0,
        desc="Epochs",
        dynamic_ncols=True,
        position=0,
        leave=True,
    )
    ebar.n = start_epoch
    pbar = tqdm(
        total=len(dl_trn),
        desc="Epoch Progress",
        dynamic_ncols=True,
        position=1,
        leave=True,
        bar_format="{desc}: {percentage:3.0f}%|{bar}| " "{n_fmt}/{total_fmt}",
    )
    ebar.set_postfix(
        acc_val=f"{val_acc:.4f}",
        acc_train=f"{trn_acc:.4f}",
        loss_val=f"{val_loss:.6f}",
        loss_train=f"{trn_loss:.6f}",
    )
    atexit.register(pbar.close)
    atexit.register(ebar.close)
    for epoch in range(start_epoch, epochs):
        model.train()
        pbar.n = 0
        pbar.refresh()
        for epoch_i, (inputs, labels) in enumerate(dl_trn):
            if torch.cuda.is_available():
                inputs = (ip.cuda() for ip in inputs)
                labels = labels.cuda()
            outputs = model(*inputs)
            loss = criterion(outputs, labels)
            opt.zero_grad()
            loss.backward()
            opt.step()

            batch_fp.log_batch(
                loss.detach().cpu(), (epoch * len(dl_trn)) + epoch_i
            )
            pbar.update()
            pbar.refresh()

        val_acc, val_loss = val_fp.validate_model(
            model, criterion, dl_val, (epoch + 1) * len(dl_trn)
        )
        trn_acc, trn_loss = val_fp.validate_model(
            model, criterion, dl_val_trn, (epoch + 1) * len(dl_trn), "trn"
        )

        scheduler.step()

        if int(epoch + 1) % save_freq == 0:
            util.save_checkpoint(
                {
                    "epoch": int(epoch + 1),
                    "model_path": model_path,
                    "state_dict": model.state_dict(),
                    "opt": opt.state_dict(),
                    "focus_offset": focus_offset,
                    "chunk_context": chunk_context,
                    "fixed_seq_len_chunks": model._variable_width_possible,
                    "mod_motif": mod_motif,
                    "base_pred": base_pred,
                },
                out_path,
            )
        ebar.set_postfix(
            acc_val=f"{val_acc:.4f}",
            acc_train=f"{trn_acc:.4f}",
            loss_val=f"{val_loss:.6f}",
            loss_train=f"{trn_loss:.6f}",
        )
        ebar.update()
    ebar.close()
    pbar.close()
    LOGGER.info("Saving final model checkpoint")
    util.save_checkpoint(
        {
            "epoch": int(epoch + 1),
            "model_path": model_path,
            "state_dict": model.state_dict(),
            "opt": opt.state_dict(),
            "focus_offset": focus_offset,
            "chunk_context": chunk_context,
            "fixed_seq_len_chunks": model._variable_width_possible,
            "mod_motif": mod_motif,
            "base_pred": base_pred,
        },
        out_path,
    )
    LOGGER.info("Training complete")


if __name__ == "__main__":
    NotImplementedError("This is a module.")
