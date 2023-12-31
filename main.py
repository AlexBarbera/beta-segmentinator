import argparse
import multiprocessing
import os.path
import pickle
import sys
import threading

import matplotlib.pyplot as plt
import matplotlib
import matplotlib.patches
import torchvision.transforms.functional
import napari
import tifffile
import torch.cuda
import torchvision.ops
import numpy
import tqdm


def parse_args():
    output = argparse.ArgumentParser("Cell segmentor using MaskRCNN for DNA channel.")

    output.add_argument("input", type=str, help="Path to input tiff file.")
    output.add_argument("output", type=str, help="Path to store output.")
    output.add_argument("--thres-nms", type=float, help="NMS threshold.", default=0.1)
    output.add_argument("--thres-prediction", type=float, help="Prediction score threshold.", default=0.6)
    output.add_argument("--thres-mask", type=float, help="Mask threshold.", default=0.95)
    output.add_argument("--thres-size", type=float, help="% Size threshold for cells relative to tile size.", default=0.3)
    output.add_argument("--tile-size", type=int, help="Size of tiles to feed the segmentor model.", default=128)
    output.add_argument("--model-path", type=str, help="Path to MaskRCNN segmentor.", default="/model.py")
    output.add_argument("--device", type=str, help="Device to run segmentation. eg \"cpu\" or \"cuda:N\" where N is gpu id.", default="cuda:0")
    output.add_argument("--rolling-window", type=int, help="How many pixels to move for each rolling window step.", default=10)
    output.add_argument("--parallel", action="store_true", help="Handle tiling in parallel. DO NOT USE!!")
    output.add_argument("--no-viewer", action="store_true", help="Do not launch the Napari viewer to visualize output.")

    return output.parse_args(sys.argv[1:])

def normalize_8_bit(image):
    if image.dtype == numpy.int8:
        return image / (2**8)  # tecnically not necessary but for completion-wise
    elif image.dtype == numpy.float16 or image.dtype == numpy.uint16:
        return image / (2**16)
    elif image.dtype == numpy.float32 or image.dtype == numpy.uint32:
        return image / (2**32)
    elif image.dtype == numpy.float64 or image.dtype == numpy.uint64:
        return image / (2**64)
    else:
        raise Exception("Invalid dtype {}".format(image.dtype))


def filter_tile(res, tile_area, i, j):
    for key in res:
        res[key] = res[key].detach().cpu()

    res["masks"] = res["masks"].numpy()
    res["boxes"] = res["boxes"].numpy()

    index = (res["scores"] >= args.thres_prediction).numpy()

    res["scores"] = res["scores"][index]
    res["masks"] = res["masks"][index]
    res["boxes"] = res["boxes"][index]

    if len(res["boxes"]) == 0:
        return res

    if res["boxes"].ndim == 1:
        res["boxes"] = res["boxes"].reshape((1, *res["boxes"].shape))

    if res["masks"].ndim == 1:
        res["masks"] = res["masks"].reshape((1, *res["masks"].shape))

    # filter big prediction boxes (consider making it a parameter)
    index = [(b[2] - b[0]) * (b[3] - b[1]) / tile_area <= args.thres_size for b in res["boxes"]]

    res["scores"] = res["scores"][index]
    res["masks"] = res["masks"][index]
    res["boxes"] = res["boxes"][index]

    if len(res["boxes"]) == 0:
        return res

    # keep only decently sized cells
    index = [(b[2] - b[0]) * (b[3] - b[1]) > 30 for b in res["boxes"]]
    res["scores"] = res["scores"][index]
    res["masks"] = res["masks"][index]
    res["boxes"] = res["boxes"][index]

    if len(res["boxes"]) == 0:
        return res

    # keep only those who have masks
    index = [mask.sum() > 30 for mask in res["masks"]]
    res["scores"] = res["scores"][index]
    res["masks"] = res["masks"][index]
    res["boxes"] = res["boxes"][index]

    if len(res["boxes"]) == 0:
        return res

    n_masks = list()

    for k in range(len(res["masks"])):
        res["masks"][k] = (res["masks"][k] >= args.thres_mask).astype(int)

        # trim mask to bounded box
        n_masks.append( ((j,i), res["masks"][k][
                                0,
                                int(res["boxes"][k][1]):int(res["boxes"][k][3]),
                                int(res["boxes"][k][0]):int(res["boxes"][k][2])
                                ]
                         ))
        #n_masks.append( ( (j, i), res["masks"][k]) )
        # update boxes coordinates from tile coord to image coord
        res["boxes"][k][0] += j
        res["boxes"][k][1] += i
        res["boxes"][k][2] += j
        res["boxes"][k][3] += i

    #del res["masks"]
    res["masks"] = n_masks

    return res


def save_intermediate_step(res, path):
    with open(path, "wb") as f:
        output = {"scores": res["scores"], "masks": res["masks"], "boxes": res["boxes"]}
        pickle.dump(output, f)


def load_all_steps(path):
    output = {"scores": list(), "masks": list(), "boxes": list()}

    for p in os.listdir(path):
        fpath = os.path.join(path, p)
        if os.path.isfile(fpath):
            with open(fpath, "rb") as f:
                temp = pickle.load(f)
                for box in temp["boxes"]:
                    if box.ndim == 1:
                        output["boxes"].append(torch.tensor(box))
                    else:
                        for b in box:
                            output["boxes"].append(torch.tensor(b))

                for score in temp["scores"]:
                    if score.ndim == 0:
                        output["scores"].append(score.view(1))
                    else:
                        output["scores"].append(score)

                for mask in temp["masks"]:
                    output["masks"].append(mask)
                """
                for key in temp:
                    for element in temp[key]:
                        if element.ndim == 2:
                            output[key].extend(element)
                        elif element.ndim != 0:
                            output[key].append(element)
                        else:
                            output[key].append(element.view(1))
                """
    output["boxes"] = torch.vstack(output["boxes"])
    output["scores"] = torch.cat(output["scores"], 0)

    return output

lock = threading.Lock()

def call_model_from_lock(model, x):
    lock.locked()
    output = model(x)[0]
    lock.release()
    return output


def extract_tile_run_model_save(args, tiff, model, coordX, coordY, counter, needs_lock=False):
    tile = torchvision.transforms.functional.crop(tiff, coordX, coordY, args.tile_size, args.tile_size)
    tile = torch.FloatTensor(tile).reshape((1, args.tile_size, args.tile_size)).cuda()
    if needs_lock:
        res = call_model_from_lock(model, [tile])
    else:
        res = model([tile])[0]
    res = filter_tile(res, args.tile_size ** 2, coordX, coordY)

    if len(res["boxes"]) != 0:
        save_intermediate_step(res, os.path.join(args.output, "step1", str(counter) + ".pkl"))


def tile_extraction_part_mt(args, tiff, model):
    pool = multiprocessing.Pool()
    width = tiff.shape[0]
    pool.apply_async(extract_tile_run_model_save, args=[
        (args, tiff, model, x, y, width * x + y)
            for x in range(0, tiff.shape[1], args.rolling_window)
            for y in range(0, tiff.shape[1], args.rolling_window)
        ]
        )
    pool.join()

def tile_extraction_part(tiff, model):
    TILE_AREA = args.tile_size ** 2
    counter = 0

    # with tqdm.tqdm(total=int((args.tile_size/args.rolling_window) ** 2), desc="Tiles done: ") as bar:
    for i in tqdm.tqdm(range(0, tiff.shape[0], args.rolling_window), desc="out"):
        if i + args.tile_size > tiff.shape[0]:
            break

        for j in tqdm.tqdm(range(0, tiff.shape[1], args.rolling_window), desc="in"):
            if j + args.tile_size > tiff.shape[1]:
                break
            """
            #tile = tiff[i: i + args.tile_size, j: j + args.tile_size].reshape((1, args.tile_size, args.tile_size))
            tile = torchvision.transforms.functional.crop(tiff, i, j, args.tile_size, args.tile_size)
            tile = torch.FloatTensor(tile).reshape((1, args.tile_size, args.tile_size)).cuda()
            res = model([tile])[0]
            res = filter_tile(res, TILE_AREA, i, j)

            if len(res["boxes"]) != 0:
                save_intermediate_step(res, os.path.join(args.output, "step1", str(counter) + ".pkl"))
            """
            extract_tile_run_model_save(args, tiff, model, i, j, counter)
            #del tile
            #del res
            counter += 1
    #            bar.update()

    if tiff.shape[0] % args.rolling_window != 0:
        # last row
        for j in tqdm.tqdm(range(0, tiff.shape[1], args.rolling_window), desc="j"):
            if j + args.tile_size >= tiff.shape[1]:
                break

            tile = torchvision.transforms.functional.crop(tiff, tiff.shape[0] - args.tile_size, j, args.tile_size, args.tile_size)
            tile = torch.FloatTensor(tile).reshape((1, args.tile_size, args.tile_size)).cuda()
            res = model([tile])[0]
            res = filter_tile(res, TILE_AREA, tiff.shape[0] - args.tile_size, j)

            save_intermediate_step(res, os.path.join(args.output, "step1", str(counter) + ".pkl"))
            counter += 1

    if tiff.shape[1] % args.rolling_window != 0:
        # last columns
        for i in tqdm.tqdm(range(0, tiff.shape[0], args.rolling_window), desc="i"):
            if i + args.tile_size >= tiff.shape[0]:
                break
            tile = torchvision.transforms.functional.crop(tiff, i, tiff.shape[1] - args.tile_size, args.tile_size, args.tile_size)
            tile = torch.FloatTensor(tile).reshape((1, args.tile_size, args.tile_size)).cuda()
            res = model([tile])[0]
            res = filter_tile(res, TILE_AREA, i, tiff.shape[1] - args.tile_size)

            save_intermediate_step(res, os.path.join(args.output, "step1", str(counter) + ".pkl"))
            counter += 1

    if tiff.shape[0] % args.rolling_window != 0 and tiff.shape[1] % args.rolling_window != 0:
        # bottom right square
        tile = torchvision.transforms.functional.crop(tiff, tiff.shape[0] - args.tile_size, tiff.shape[1] - args.tile_size, args.tile_size,
                                                      args.tile_size)
        tile = torch.FloatTensor(tile).reshape((1, args.tile_size, args.tile_size)).cuda()
        res = model([tile])[0]
        res = filter_tile(res, TILE_AREA, tiff.shape[0] - args.tile_size, tiff.shape[1] - args.tile_size)

        save_intermediate_step(res, os.path.join(args.output, "step1", str(counter) + ".pkl"))
        counter += 1


def plot_full(tiff, boxes, scores):
    plt.imshow(tiff)
    ax = plt.gca()
    for i  in range(len(boxes)):
        ax.add_patch(
            matplotlib.patches.Rectangle((boxes[i][0], boxes[i][1]), boxes[i][2] - boxes[i][0], boxes[i][3]-boxes[i][1], fill=False, alpha=1, color="red")
        )
        plt.text(boxes[i][0], boxes[i][1], str(scores[i]), fontsize=8, color="white")
    plt.show()


def pipeline(args):
    device = "cpu"
    original_shape = None

    if "cuda" in args.device:
        if int(args.device.split(":")[-1]) < torch.cuda.device_count():
            device = args.device
        else:
            print("Invalid gpu id. Detected {} but id {} was selected.".format(torch.cuda.device_count(), int(args.device.split(":")[-1])))
            sys.exit(1)

    tiff = tifffile.imread(args.input)
    # grab first channel if multiple channels available
    if tiff.ndim == 3 and tiff.shape[0] > 1:  # TODO this assumes first channel is DAPI, pass as parameter?
        tiff = tiff[0]
    original_shape = tiff.shape

    if len(os.listdir(os.path.join(args.output, "step1"))) == 0 or True:
        print("No previous run found")
        os.makedirs(os.path.join(args.output, "step1"), exist_ok=True)

        model = torch.load(args.model_path, map_location=torch.device(device))
        model.eval()

        # iterate tiff by tile size
        # strategy would be to load 3x3 square surrounding current tile to ensure we have all overlapping predictions for current tile
        # first step is to filter out predictions outside current tile (remeber to adapt x,y coordinates for adjacent tiles)
        # Then apply size threshold
        # apply prediction threshold followed by nms threshold
        # store after binary mask threshold
        # next tile...

        tiff = normalize_8_bit(tiff) * 255.0
        tiff = torch.FloatTensor(tiff.astype(numpy.float16))

        if args.parallel:
            torch.multiprocessing.set_start_method("spawn", force=True)
            tile_extraction_part_mt(args, tiff, model)
        else:
            tile_extraction_part(tiff, model)

        # free some memory
        del model
    del tiff

    output = load_all_steps(os.path.join(args.output, "step1"))

    indexes = torchvision.ops.nms(output["boxes"], output["scores"], args.thres_nms).numpy()

    output["boxes"] = output["boxes"][indexes]
    output["scores"] = output["scores"][indexes]
    temp = list()
    for i in indexes:
        temp.append(output["masks"][i])
    output["masks"] = temp

    #plot_full(tiff, output["boxes"], output["scores"])

    final = numpy.zeros(original_shape)
    print(final.shape)

    for i in range(len(output["boxes"])):
        box = output["boxes"][i].type(torch.int)
        anchor, mask = output["masks"][i]
        #mask = mask.reshape((args.tile_size, args.tile_size))
        mask = mask * (i + 1)
        if box[3] - box[1] != mask.shape[0] or box[2] - box[0] != mask.shape[1]:  # TODO wtf is this case?
            print(box, mask.shape)
            mask = mask[0:min(mask.shape[0], box[3] - box[1]), 0:min(mask.shape[1], box[2] - box[0])]
            final[box[1]:box[1]+mask.shape[0], box[0]:box[0]+mask.shape[1]] += mask
        else:
            # TODO make changes that adapt to overlapping boxes and masks, XOR?
            final[box[1]:box[3], box[0]:box[2]] += mask

    if not args.no_viewer:
        viewer = napari.Viewer()
        viewer.add_image(tifffile.imread(args.input))
        shapes = viewer.add_shapes(
            [
                [
                    [box[1].item(), box[0].item()],
                    [box[3].item(), box[2].item()]
                ] for box in output["boxes"]
            ],
            edge_width=2,
            edge_color="coral",
            text={"string": "{scores:.4f}", "anchor":"center", "color":"red", "size":6},
            features={"scores":[x.item() for x in output["scores"]]},
            blending="translucent",
            name="Bounding Boxes",
            opacity=1.0,
            shape_type="rectangle",
            face_color="transparent"
        )
        mask = viewer.add_labels(
            final.astype(int), name="Masks"
        )

        viewer.show(block=True)

    tifffile.imwrite(os.path.join(args.output, "output.tiff"), final)

    print("Done :)")


if __name__ == "__main__":
    args = parse_args()
    print(args)

    if torch.cuda.device_count() == 0 and args.device == "cuda":
        print("Pytorch cannot find GPU devices (and gpu device is selected).")
        sys.exit(1)

    if not os.path.isfile(args.input):
        print("Input file not found {}.".format(args.input))
        sys.exit(1)

    if not os.path.isfile(args.model_path):
        print("Model file not found {}.".format(args.model_path))
        sys.exit(1)

    if args.parallel:
        print("Parallel implementation not ready yet... sorry :(")
        sys.exit(1)

    if not os.path.exists(args.output):
        os.makedirs(args.output, exist_ok=True)

    with torch.no_grad():
        pipeline(args)
