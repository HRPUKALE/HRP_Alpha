# %%
#  Imports
# OS and IO
import os
import sys
import pickle
import subprocess
import matplotlib.pyplot as plt
import matplotlib as mpl
import numpy as np
import dask.array as da
from sklearn.preprocessing import StandardScaler
import torch
import dask
import json
import zarr as zr

# from dask import optimize
import time


# Backend Libraries
import joblib as jl

from utils import toolbox, ML_functions as mlf
import metrics, baselines

# Importing the sklearn metrics
from sklearn.metrics import root_mean_squared_error, mean_absolute_error
import argparse

# %%
if __name__ == "__main__":
    # emulate system arguments
    emulate = False
    # Simulate command line arguments
    if emulate:
        sys.argv = [
            "script_name",  # Traditionally the script name, but it's arbitrary in Jupyter
            "--ai_model",
            "fourcastnetv2",
            "--overwrite_cache",
            # "--min_leadtime",
            # "6",
            # "--max_leadtime",
            # "24",
            # "--use_gpu",
            "--verbose",
            "--cache_dir",
            "/scratch/mgomezd1/cache",
            "--mask",
        ]
    # %%
    # check if the context has been set for torch multiprocessing
    if torch.multiprocessing.get_start_method() != "spawn":
        torch.multiprocessing.set_start_method("spawn", force=True)

    # Read in arguments with argparse
    parser = argparse.ArgumentParser(description="Train a CNN model")
    parser.add_argument(
        "--datadir",
        type=str,
        default="/work/FAC/FGSE/IDYST/tbeucler/default/raw_data/TCBench_alpha",
        help="Directory where the data is stored",
    )
    parser.add_argument(
        "--cache_dir",
        type=str,
        default="/scratch/mgomezd1/cache",
        help="Directory where the cache is stored",
    )

    parser.add_argument(
        "--result_dir",
        type=str,
        default="/work/FAC/FGSE/IDYST/tbeucler/default/milton/repos/alpha_bench/dev/results/",
        help="Directory where the results are stored",
    )

    parser.add_argument(
        "--ignore_gpu",
        action="store_true",
        help="Whether to ignore the GPU for training",
    )

    parser.add_argument(
        "--ai_model",
        type=str,
        default="panguweather",
    )

    parser.add_argument(
        "--mode",
        type=str,
        default="deterministic",
    )

    parser.add_argument(
        "--deterministic_loss",
        type=str,
        default="RMSE",
    )

    parser.add_argument(
        "--overwrite_cache",
        help="Enable cache overwriting",
        action="store_true",
    )

    parser.add_argument(
        "--verbose",
        help="Enable verbose mode",
        action="store_true",
    )

    parser.add_argument(
        "--debug",
        help="Enable debug mode",
        action="store_true",
    )

    parser.add_argument(
        "--min_leadtime",
        type=int,
        default=6,
    )

    parser.add_argument(
        "--max_leadtime",
        type=int,
        default=168,
    )

    parser.add_argument(
        "--cnn_width",
        type=str,
        default="[32,64,128]",
    )

    parser.add_argument(
        "--dropout",
        type=float,
        default=0.25,
    )

    parser.add_argument(
        "--ablate_cols",
        type=str,
        default="[]",
    )

    parser.add_argument(
        "--fc_width",
        type=int,
        default=512,
    )

    parser.add_argument(
        "--magAngle_mode",
        action="store_true",
        help="Whether to use magnitude and angle instead of u and v",
    )

    parser.add_argument(
        "--raw_target",
        action="store_true",
        help="Whether to use intensity instead of intensification",
    )

    parser.add_argument(
        "--reanalysis",
        action="store_true",
        help="Whether to use reanalysis data instead of forecast data",
    )

    parser.add_argument(
        "--mask",
        action="store_true",
        help="Whether to apply a leadtime driven mask to the data",
    )

    parser.add_argument(
        "--mask_path",
        type=str,
        default="/work/FAC/FGSE/IDYST/tbeucler/default/milton/repos/alpha_bench/dev/results/mask_dict.pkl",
    )

    parser.add_argument(
        "--mask_type",
        type=str,
        default="linear",
    )

    parser.add_argument(
        "--aux_loss",
        action="store_true",
        help="Whether to use an auxiliary loss",
    )

    args = parser.parse_args()

    print("Imports successful", flush=True)

    # print the multiprocessing start method
    print(torch.multiprocessing.get_start_method(allow_none=True))
    dask.config.set(scheduler="processes")

    #  Setup
    datadir = args.datadir
    cache_dir = args.cache_dir + f"_{args.ai_model}"
    result_dir = args.result_dir

    # Check for GPU availability
    if torch.cuda.is_available() and not args.ignore_gpu:
        calc_device = torch.device("cuda:0")
    else:
        calc_device = torch.device("cpu")

    num_cores = int(subprocess.check_output(["nproc"], text=True).strip())

    #  Data Loading
    rng = np.random.default_rng(seed=2020)

    years = list(range(2013, 2020))
    rng.shuffle(years)

    # Make the cache directory if it doesn't exist
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir)

    if args.reanalysis:
        sets, data = toolbox.get_reanal_sets(
            {
                "train": years[:-2],
                "validation": years[-2:],
                # "test": [2020],
            },
            datadir=datadir,
            test_strategy="custom",
            base_position=True,
            cache_dir=cache_dir,
            # use_cached=not args.overwrite_cache,
            verbose=args.verbose,
            debug=args.debug,
        )
    else:
        print("Loading datasets...", flush=True)
        sets, data = toolbox.get_ai_sets(
            {
                "train": years[:-2],
                "validation": years[-2:],
                # "test": [2020],
            },
            datadir=datadir,
            test_strategy="custom",
            base_position=True,
            ai_model=args.ai_model,
            cache_dir=cache_dir,
            # use_cached=not args.overwrite_cache,
            verbose=args.verbose,
            debug=args.debug,
        )

    # %%
    # create a mask for the leadtimes
    train_ldt_mask = da.logical_and(
        data["train"]["leadtime"] >= args.min_leadtime,
        data["train"]["leadtime"] <= args.max_leadtime,
    )
    train_ldt_mask = np.squeeze(train_ldt_mask.compute(scheduler="threads"))

    validation_ldt_mask = da.logical_and(
        data["validation"]["leadtime"] >= args.min_leadtime,
        data["validation"]["leadtime"] <= args.max_leadtime,
    )
    validation_ldt_mask = np.squeeze(validation_ldt_mask.compute(scheduler="threads"))

    #  Preprocessing

    # Let's filter the data using leadtimes
    print("Filtering data...", flush=True)
    train_data = data["train"]["inputs"][train_ldt_mask]
    valid_data = data["validation"]["inputs"][validation_ldt_mask]
    # and the columns to ablate
    ablate_cols = json.loads(args.ablate_cols)

    if len(ablate_cols) > 0:
        train_data = train_data[
            :,
            [i for i in range(train_data.shape[1]) if i not in ablate_cols],
            :,
            :,
        ]
        valid_data = valid_data[
            :,
            [i for i in range(valid_data.shape[1]) if i not in ablate_cols],
            :,
            :,
        ]

    if args.magAngle_mode:
        train_data = mlf.uv_to_magAngle(train_data, u_idx=0, v_idx=1)
        valid_data = mlf.uv_to_magAngle(valid_data, u_idx=0, v_idx=1)

    # We will want to normalize the inputs for the model
    # to work properly. We will use the AI_StandardScaler for this
    # purpose
    AI_scaler = None
    from_cache = not args.overwrite_cache
    fpath = os.path.join(cache_dir, "AI_scaler.pkl")

    if from_cache:
        if os.path.exists(fpath):
            print("Loading AI scaler from cache...", flush=True)
            with open(fpath, "rb") as f:
                AI_scaler = pickle.load(f)

    if AI_scaler is None:
        print("Fitting AI datascaler...", flush=True)
        # AI_data = optimize(AI_data)[0]
        AI_scaler = mlf.AI_StandardScaler()
        AI_scaler.fit(train_data, num_workers=num_cores)

        # save the scaler to the cache
        with open(fpath, "wb") as f:
            pickle.dump(AI_scaler, f)

    # We also want to do the same for the base intensity
    base_scaler = None
    from_cache = not args.overwrite_cache
    fpath = os.path.join(cache_dir, "base_scaler.pkl")

    if from_cache:
        if os.path.exists(fpath):
            print("Loading base scaler from cache...", flush=True)
            with open(fpath, "rb") as f:
                base_scaler = pickle.load(f)

    if base_scaler is None:
        print("Fitting base intensity scaler...", flush=True)
        base_scaler = StandardScaler()
        base_scaler.fit(data["train"]["base_intensity"][train_ldt_mask])

        # save the scaler to the cache
        with open(fpath, "wb") as f:
            pickle.dump(base_scaler, f)

    # and one for the base position
    print("Encoding base position...", flush=True)
    train_positions = mlf.latlon_to_sincos(
        data["train"]["base_position"][train_ldt_mask]
    ).compute()
    valid_positions = mlf.latlon_to_sincos(
        data["validation"]["base_position"][validation_ldt_mask]
    ).compute()

    # We'll encode the leadtime by dividing it by the max leadtime in the dataset
    # which is 168 hours
    print("Encoding leadtime...", flush=True)
    max_train_ldt = data["train"]["leadtime"][train_ldt_mask].max().compute()
    train_leadtimes = (
        data["train"]["leadtime"][train_ldt_mask] / max_train_ldt
    ).compute(scheduler="threads")
    validation_leadtimes = (
        data["validation"]["leadtime"][validation_ldt_mask] / max_train_ldt
    ).compute(schedule="threads")

    if args.raw_target:
        train_target = data["train"]["outputs"][train_ldt_mask]
        valid_target = data["validation"]["outputs"][validation_ldt_mask]
    else:
        # We also want to precalculate the delta intensity for the
        # training and validation sets
        print("Calculating target (i.e., delta intensities)...", flush=True)
        train_target = (
            data["train"]["outputs"][train_ldt_mask]
            - data["train"]["base_intensity"][train_ldt_mask]
        )
        valid_target = (
            data["validation"]["outputs"][validation_ldt_mask]
            - data["validation"]["base_intensity"][validation_ldt_mask]
        )

    # Save the training and validation inputs as zarrays in the cache
    train_data.rechunk((1000, -1, -1, -1)).to_zarr(
        os.path.join(cache_dir, "train_data.zarr"), overwrite=args.overwrite_cache
    )
    valid_data.rechunk((1000, -1, -1, -1)).to_zarr(
        os.path.join(cache_dir, "valid_data.zarr"), overwrite=args.overwrite_cache
    )

    train_zarr = zr.open(
        os.path.join(cache_dir, "train_data.zarr"), mode="a", chunks=(1000, -1, -1, -1)
    )
    valid_zarr = zr.open(
        os.path.join(cache_dir, "valid_data.zarr"), mode="a", chunks=(1000, -1, -1, -1)
    )
    # %%
    # Loading the mask to apply that depends on the leadtime
    if args.mask:
        mask_path = args.mask_path
        with open(mask_path, "rb") as f:
            mask_dict = pickle.load(f)[args.mask_type]

        print("Applying radial mask...", flush=True)
        unique_leads = da.unique(data["train"]["leadtime"]).compute()

        train_mask = da.ones((train_data.shape[0], 241, 241), chunks=(1000, -1, -1))
        valid_mask = da.ones((valid_data.shape[0], 241, 241), chunks=(1000, -1, -1))

        for lead in unique_leads:
            radial_mask = mask_dict[lead]

            temp_ldt_mask_train = (
                (data["train"]["leadtime"] == lead).compute().squeeze()
            )
            temp_ldt_mask_valid = (
                (data["validation"]["leadtime"] == lead).compute().squeeze()
            )

            train_mask[temp_ldt_mask_train] = (
                train_mask[temp_ldt_mask_train] * radial_mask
            )
            valid_mask[temp_ldt_mask_valid] = (
                valid_mask[temp_ldt_mask_valid] * radial_mask
            )

        train_mask = train_mask.rechunk((1000, -1, -1))
        valid_mask = valid_mask.rechunk((1000, -1, -1))

        # Define the lambda function to apply the mask
        apply_mask = lambda chunk, mask: chunk * mask

        # # Apply the mask to each chunk
        # train_data_processed = da.map_blocks(apply_mask, train_data, train_mask)
        # valid_data_processed = da.map_blocks(apply_mask, valid_data, valid_mask)

        print("Starting radial mask on-disk compute...")
        da.map_blocks(apply_mask, train_data, train_mask).compute(schedule="threads")
        da.map_blocks(apply_mask, valid_data, valid_mask).compute(schedule="threads")

        # train_zarr[temp_ldt_mask_train] = (
        #     train_data[temp_ldt_mask_train] * radial_mask
        # ).compute(scheduler="threads")
        # train_zarr[temp_ldt_mask_valid] = (
        #     valid_data[temp_ldt_mask_valid] * radial_mask
        # ).compute(scheduler="threads")

        # train_shape = train_data[data["train"]["leadtime"][train_ldt_mask] == lead]

        # .shape()
        # valid_shape = valid_data[data["validation"]["leadtime"] == lead].shape()

        # train_data[data["train"]["leadtime"] == lead] = train_data[
        #     data["train"]["leadtime"] == lead
        # ] * da.tile(radial_mask, (train_shape[0], train_shape[1], 1, 1))
        # valid_data[data["validation"]["leadtime"] == lead] = valid_data[
        #     data["validation"]["leadtime"] == lead
        # ] * da.tile(radial_mask, (valid_shape[0], valid_shape[1], 1, 1))
        print("Radial mask applied", flush=True)
        train_data = da.from_zarr(train_zarr)
        valid_data = da.from_zarr(valid_zarr)
    # And scale the target data using the cached scaler if available
    target_scaler = None
    from_cache = not args.overwrite_cache
    fpath = os.path.join(cache_dir, "target_scaler.pkl")

    if from_cache:
        if os.path.exists(fpath):
            print("Loading target scaler from cache...", flush=True)
            with open(fpath, "rb") as f:
                target_scaler = pickle.load(f)

    if target_scaler is None:
        print("Fitting target scaler...", flush=True)
        target_scaler = StandardScaler()
        target_scaler.fit(train_target)

        # save the scaler to the cache
        with open(fpath, "wb") as f:
            pickle.dump(target_scaler, f)

    #  Dataloader & Hyperparameters

    # Let's define some hyperparameters
    batch_size = 32

    # If the mode is not deterministic, we'll set the loss to CRPS
    if args.mode != "deterministic":
        loss_func = metrics.CRPS_ML
    else:
        if args.deterministic_loss == "RMSE":
            loss_func = torch.nn.MSELoss()
        elif args.deterministic_loss == "MAE":
            loss_func = torch.nn.L1Loss()
        else:
            raise ValueError("Loss function not recognized.")

    num_workers = (
        int(num_cores * 2 / 3)
        if (calc_device == torch.device("cpu") and num_cores > 1)
        else num_cores
    )

    # We then instantiate the DaskDataset class for the training and validation sets.
    # Validation first because it's smaller and will be used to evaluate if the code
    # is working as expected
    print("Creating validation DaskDataset and dataloader...", flush=True)
    validation_dataset = mlf.DaskDataset(
        AI_X=valid_data,
        AI_scaler=AI_scaler,
        base_int=data["validation"]["base_intensity"][validation_ldt_mask],
        base_scaler=base_scaler,
        target_data=valid_target,
        target_scaler=target_scaler,
        device=calc_device,
        cachedir=cache_dir,
        zarr_name="validation",
        overwrite=args.overwrite_cache,
        num_workers=num_cores,
        track=valid_positions,
        leadtimes=validation_leadtimes,
        # load_into_memory=True,
    )

    validation_loader = mlf.make_dataloader(
        validation_dataset,
        batch_size=batch_size,
        shuffle=False,
        num_workers=num_workers,
        pin_memory=True,
    )

    print("Creating training DaskDataset and dataloader...", flush=True)
    train_dataset = mlf.DaskDataset(
        AI_X=train_data,
        AI_scaler=AI_scaler,
        base_int=data["train"]["base_intensity"][train_ldt_mask],
        base_scaler=base_scaler,
        target_data=train_target,
        target_scaler=target_scaler,
        device=calc_device,
        cachedir=cache_dir,
        zarr_name="training",
        overwrite=args.overwrite_cache,
        num_workers=num_cores,
        track=train_positions,
        leadtimes=train_leadtimes,
        # load_into_memory=True,
    )
    train_loader = mlf.make_dataloader(
        train_dataset,
        batch_size=batch_size,
        shuffle=True,
        num_workers=num_workers,
        pin_memory=True,
    )

    if emulate:
        input("Press Enter to continue...")
    #  Model
    # We begin by instantiating our baseline model
    CNN = baselines.RegularizedCNN(
        deterministic=True if args.mode == "deterministic" else False,
        num_scalars=train_dataset.num_scalars,
        input_cols=5 - len(json.loads(args.ablate_cols)),
        cnn_widths=json.loads(args.cnn_width),
        fc_width=args.fc_width,
        dropout=args.dropout,
        dropout2d=args.dropout,
        aux_loss=args.aux_loss,
    ).to(calc_device)
    # CNN = baselines.SimpleCNN(
    #     deterministic=True if args.mode == "deterministic" else False,
    #     num_scalars=train_dataset.num_scalars,
    #     input_cols=5 - len(json.loads(args.ablate_cols)),
    #     cnn_widths=json.loads(args.cnn_width),
    #     fc_width=args.fc_width,
    #     aux_loss=args.aux_loss,
    # ).to(calc_device)

    optimizer = torch.optim.Adam(CNN.parameters(), lr=1e-4)  # , weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CyclicLR(
        optimizer, base_lr=1e-5, max_lr=1e-3, step_size_up=10
    )

    num_epochs = 100
    patience = 5  # stop if validation loss increases for patience epochs
    bias_threshold = 30  # stop if validation loss / train loss > bias_threshold

    #  Training
    train_losses = []
    val_losses = []
    if args.aux_loss:
        train_base_losses = []
        val_base_losses = []
        train_aux_losses = []
        val_aux_losses = []

    print("Model Training started...", flush=True)
    start_time = time.strftime("%m-%d-%Hh%M")
    for epoch in range(num_epochs):
        train_loss = 0
        if args.aux_loss:
            train_base_loss = 0
            train_aux_loss = 0
        i = 0
        print("\nTraining:", flush=True)
        for AI_X, scalars, target in train_loader:
            i += 1
            optimizer.zero_grad()

            # batch_data = AI_X.to(calc_device)
            # batch_scalars = scalars.to(calc_device)
            # prediction = CNN(
            #     x=batch_data,
            #     scalars=batch_scalars,
            # )
            # print(AI_X.device, scalars.device)
            AI_X = AI_X.to(calc_device, non_blocking=True)
            scalars = scalars.to(calc_device, non_blocking=True)
            target = target.to(calc_device, non_blocking=True)
            # print(AI_X.device, scalars.device)

            prediction = CNN(x=AI_X, scalars=scalars)

            if args.aux_loss:
                base_loss = loss_func(
                    prediction[:, : CNN.target_size],
                    target,
                )
                aux_loss = loss_func(
                    prediction[:, CNN.target_size :],
                    base_loss,
                )
                batch_loss = base_loss + aux_loss

            else:
                batch_loss = loss_func(prediction, target)

            batch_loss.backward()

            optimizer.step()

            train_loss += batch_loss.item()
            if args.aux_loss:
                train_base_loss += base_loss.item()
                train_aux_loss += aux_loss.item()

            # print a simple progress bar that shows a dot for each percent of the batch
            print(
                (
                    "\r"
                    + f"{i}/{len(train_loader)}"
                    + "." * int(20 * i / len(train_loader))
                    + f" Batch loss: {batch_loss.item():.4f}"
                    + (
                        ""
                        if not args.aux_loss
                        else f" Base loss: {base_loss.item():.4f}, Aux loss: {aux_loss.item():.4f}"
                    )
                ),
                end="",
                flush=True,
            )
        scheduler.step()

        train_loss = train_loss / len(train_loader)
        train_losses.append(train_loss)
        if args.aux_loss:
            train_base_loss = train_base_loss / len(train_loader)
            train_aux_loss = train_aux_loss / len(train_loader)
            train_base_losses.append(train_base_loss)
            train_aux_losses.append(train_aux_loss)

        print("\nValidation:", flush=True)
        val_loss = 0
        if args.aux_loss:
            val_base_loss = 0
            val_aux_loss = 0
        i = 0
        with torch.no_grad():
            for AI_X, scalars, target in validation_loader:
                print(
                    f"\r {AI_X.dtype} {scalars.dtype} {target.dtype}",
                    end="",
                    flush=True,
                )
                i += 1
                # batch_data = AI_X.to(calc_device)
                # batch_scalars = scalars.to(calc_device)
                # prediction = CNN(
                #     x=batch_data,
                #     scalars=batch_scalars,
                # )

                AI_X = AI_X.to(calc_device, non_blocking=True)
                scalars = scalars.to(calc_device, non_blocking=True)
                target = target.to(calc_device, non_blocking=True)
                prediction = CNN(x=AI_X, scalars=scalars)

                if args.aux_loss:
                    base_loss = loss_func(
                        prediction[:, : CNN.target_size],
                        target,
                    )
                    aux_loss = loss_func(
                        prediction[:, CNN.target_size :],
                        base_loss,
                    )
                    batch_loss = base_loss + aux_loss
                else:
                    batch_loss = loss_func(prediction, target)

                val_loss += batch_loss.item()
                if args.aux_loss:
                    val_base_loss += base_loss.item()
                    val_aux_loss += aux_loss.item()

                # print a simple progress bar that shows a dot for each percent of the batch
                print(
                    (
                        "\r"
                        + f"{i}/{len(validation_loader)}"
                        + "." * int(20 * i / len(validation_loader))
                        + (
                            ""
                            if not args.aux_loss
                            else f" Base loss: {base_loss.item():.4f}, Aux loss: {aux_loss.item():.4f}"
                        )
                    ),
                    end="",
                    flush=True,
                )

        val_loss = val_loss / len(validation_loader)
        val_losses.append(val_loss)
        if args.aux_loss:
            val_base_loss = val_base_loss / len(validation_loader)
            val_aux_loss = val_aux_loss / len(validation_loader)
            val_base_losses.append(val_base_loss)
            val_aux_losses.append(val_aux_loss)

        # Save if the validation loss is the best so far
        if val_loss <= max(val_losses):
            torch.save(
                CNN,
                os.path.join(
                    result_dir,
                    f"best_model_{str(CNN)}_{start_time}_epoch-{epoch+1}_{args.ai_model}_{args.mode}_{args.cnn_width}.pt",
                ),
            )

        print(
            f"\nEpoch: {epoch+1}/{num_epochs},",
            f"train_loss: {train_loss},",
            f"val_loss: {val_loss},",
            flush=True,
            sep=" ",
        )

        # Early stopping
        # Stop if the validation loss has been increasing on average for the last patience epochs
        if epoch > patience:
            if (
                np.mean(np.gradient(val_losses)[-patience:]) > 0
                or val_loss / train_loss > bias_threshold
            ):
                print(f"Early stopping at epoch {epoch+1}")
                break

        # Let's save the train and validation losses in a pickled dictionary
        losses = {"train": train_losses, "validation": val_losses}
        if args.aux_loss:
            losses["train_base"] = train_base_losses
            losses["train_aux"] = train_aux_losses
            losses["validation_base"] = val_base_losses
            losses["validation_aux"] = val_aux_losses
        with open(
            os.path.join(
                result_dir,
                f"CNN_{str(CNN)}_losses_{start_time}_{args.ai_model}_{args.mode}_{args.cnn_width}.pkl",
            ),
            "wb",
        ) as f:
            pickle.dump(losses, f)

        # plot the learning curves
        fig, ax = plt.subplots()
        ax.plot(
            train_losses,
            label="Train Loss",
            color=np.array([27, 166, 166]) / 255,
            alpha=0.8,
        )
        ax.plot(
            val_losses,
            label="Validation Loss",
            color=np.array([191, 6, 92]) / 255,
            alpha=0.8,
        )
        ax.set_xlabel("Epoch")
        tick_space = len(train_losses) // 10
        ax.xaxis.set_ticks(
            np.arange(0, len(train_losses), tick_space if tick_space > 1 else 1)
        )
        ax.set_ylabel(f"Loss: {str(loss_func)}")
        ax.legend()
        toolbox.plot_facecolors(fig=fig, axes=ax)
        fig.savefig(
            os.path.join(
                result_dir, f"CNN_{str(CNN)}_{args.ai_model}_losses_{start_time}.png"
            )
        )

    # %%

    # def eval_prediction(
    #     model,  # ML model
    #     loader,  # Dataloader
    #     target_scaler,  # Target scaler
    #     baseline_pred,  # Baseline prediction
    #     y_true,  # Ground truth
    #     result_dir,  # Results directory
    #     start_time,  # Start time
    #     set_type,  # Type of dataset
    #     data,  # Data dictionary
    #     **kwargs,
    # ):
    #     # Predict the validation data with the CNN model
    #     with torch.no_grad():
    #         y_hat = None
    #         for AI_X, base_int, target in loader:
    #             pred = model(AI_X, base_int)
    #             if y_hat is None:
    #                 y_hat = pred.cpu().numpy()
    #             else:
    #                 y_hat = np.concatenate([y_hat, pred.cpu().numpy()])

    #     # reverse transform y_hat
    #     y_hat = target_scaler.inverse_transform(y_hat)

    #     #  Compute y_true, y_baseline
    #     y_baseline = baseline_pred.compute()

    #     #  Evaluation
    #     # We will want to evaluate the model using the Root Mean Squared Error and
    #     # the Mean Absolute Error, as well as the associated skill scores compared
    #     # to the persistence model

    #     global_performance = metrics.summarize_performance(
    #         y_true,  # Ground truth
    #         y_hat,  # Model prediction
    #         y_baseline,  # Baseline, used for skill score calculations
    #         [root_mean_squared_error, mean_absolute_error],
    #     )

    #     # Plotting Global Performance
    #     fig, axes = plt.subplots(
    #         1, 2, figsize=(15, 5), dpi=150, gridspec_kw={"width_ratios": [2, 1]}
    #     )
    #     metrics.plot_performance(
    #         global_performance,
    #         axes,
    #         model_name=f"{str(CNN)}",
    #         baseline_name=kwargs.get("baseline_name", "Persistence"),
    #     )
    #     toolbox.plot_facecolors(fig=fig, axes=axes)
    #     # Save the figure in the results directory
    #     fig.savefig(
    #         os.path.join(
    #             result_dir,
    #             f"CNN_global_performance_{str(model)}_{start_time}_{set_type}.png",
    #         )
    #     )

    #     # Per leadtime analysis
    #     unique_leads = data[set_type]["leadtime"].compute()
    #     num_leads = len(unique_leads)
    #     # set up the axes for the lead time plots

    #     fig, axes = plt.subplots(
    #         num_leads,  # One row per lead time
    #         2,
    #         figsize=(15, 5 * num_leads),
    #         dpi=150,
    #         gridspec_kw={"width_ratios": [2, 1]},
    #     )
    #     for idx, lead in enumerate(unique_leads):
    #         lead_mask = data[set_type]["leadtime"] == lead

    #         y_true_lead = y_true[lead_mask]
    #         y_hat_lead = y_hat[lead_mask]
    #         y_baseline_lead = y_baseline[lead_mask]

    #         lead_performance = metrics.summarize_performance(
    #             y_true_lead,  # Ground truth
    #             y_hat_lead,  # Model prediction
    #             y_baseline_lead,  # Baseline, used for skill score calculations
    #             [root_mean_squared_error, mean_absolute_error],
    #         )

    #         metrics.plot_performance(
    #             lead_performance,
    #             axes[idx],
    #             model_name=f"{str(model)}",
    #             baseline_name="Persistence",
    #         )
    #         toolbox.plot_facecolors(fig=fig, axes=axes[idx])

    #         # Append the lead time to the title for both axes
    #         axes[idx][0].set_title(
    #             f"\n Lead Time: +{lead}h \n" + axes[idx][0].get_title()
    #         )
    #         axes[idx][1].set_title(
    #             f"\n Lead Time: +{lead}h \n" + axes[idx][1].get_title()
    #         )

    #     # Save the figure in the results directory
    #     fig.savefig(
    #         os.path.join(
    #             result_dir,
    #             f"CNN_lead_performance_{str(model)}_{start_time}_{set_type}.png",
    #         )
    #     )

    # # Evaluate on the validation set

    # # We want to compare the model to a simple baseline, in this case
    # # the persistence model. Since our target is delta, persistence
    # # is simply 0
    # y_persistence = np.zeros_like(valid_delta)

    # #  Evaluation
    # # Let's start by loading the validation outputs into a variable
    # # for easier access
    # y_true = valid_delta

    # eval_prediction(
    #     model=CNN,
    #     loader=validation_loader,
    #     target_scaler=target_scaler,
    #     baseline_pred=y_persistence,
    #     y_true=y_true,
    #     result_dir=result_dir,
    #     start_time=start_time,
    #     set_type="validation",
    #     data=data,
    # )
    # # Evaluate on the training set

    # # We want to compare the model to a simple baseline, in this case
    # # the persistence model. Since our target is delta, persistence
    # # is simply 0
    # y_persistence = np.zeros_like(train_delta)

    # #  Evaluation
    # # Let's start by loading the validation outputs into a variable
    # # for easier access
    # y_true = train_delta

    # eval_prediction(
    #     model=CNN,
    #     loader=train_loader,
    #     target_scaler=target_scaler,
    #     baseline_pred=y_persistence,
    #     y_true=y_true,
    #     result_dir=result_dir,
    #     start_time=start_time,
    #     set_type="train",
    #     data=data,
    # )
