#!/usr/bin/env python3
import os
import pdb
import logging
import torch
import trimesh
import glob
import lib.workspace as ws
import numpy as np
import imageio

def process_image(images_out, alpha_out):
    image_out_export = 255*images_out.detach().cpu().numpy()[0].transpose((1, 2, 0))  # [image_size, image_size, RGB]
    alpha_out_export = 255*alpha_out.detach().cpu().numpy()[0]
    image_out_export = np.concatenate( (image_out_export, alpha_out_export[:,:,np.newaxis]), -1 )
    return image_out_export.astype(np.uint8)

def store_image(image_filename, images_out, alpha_out):
    image_out_export = process_image(images_out, alpha_out)
    imageio.imwrite(image_filename, image_out_export)

def interpolate_on_faces(field, faces):
    #TODO: no batch support for now
    nv = field.shape[0]
    nf = faces.shape[0]
    field = field.reshape((nv, 1))
    # pytorch only supports long and byte tensors for indexing
    face_coordinates = field[faces.long()].squeeze(0)
    centroids = 1.0/3 * torch.sum(face_coordinates, 1)
    return centroids

class LearningRateSchedule:
    def get_learning_rate(self, epoch):
        pass


class ConstantLearningRateSchedule(LearningRateSchedule):
    def __init__(self, value):
        self.value = value

    def get_learning_rate(self, epoch):
        return self.value


class StepLearningRateSchedule(LearningRateSchedule):
    def __init__(self, initial, interval, factor):
        self.initial = initial
        self.interval = interval
        self.factor = factor

    def get_learning_rate(self, epoch):

        return self.initial * (self.factor ** (epoch // self.interval))


class WarmupLearningRateSchedule(LearningRateSchedule):
    def __init__(self, initial, warmed_up, length):
        self.initial = initial
        self.warmed_up = warmed_up
        self.length = length

    def get_learning_rate(self, epoch):
        if epoch > self.length:
            return self.warmed_up
        return self.initial + (self.warmed_up - self.initial) * epoch / self.length


def get_learning_rate_schedules(specs):

    schedule_specs = specs["LearningRateSchedule"]

    schedules = []

    for schedule_specs in schedule_specs:

        if schedule_specs["Type"] == "Step":
            schedules.append(
                StepLearningRateSchedule(
                    schedule_specs["Initial"],
                    schedule_specs["Interval"],
                    schedule_specs["Factor"],
                )
            )
        elif schedule_specs["Type"] == "Warmup":
            schedules.append(
                WarmupLearningRateSchedule(
                    schedule_specs["Initial"],
                    schedule_specs["Final"],
                    schedule_specs["Length"],
                )
            )
        elif schedule_specs["Type"] == "Constant":
            schedules.append(ConstantLearningRateSchedule(schedule_specs["Value"]))

        else:
            raise Exception(
                'no known learning rate schedule of type "{}"'.format(
                    schedule_specs["Type"]
                )
            )

    return schedules


def save_model(experiment_directory, filename, decoder, epoch):

    model_params_dir = ws.get_model_params_dir(experiment_directory, True)

    torch.save(
        {"epoch": epoch, "model_state_dict": decoder.state_dict()},
        os.path.join(model_params_dir, filename),
    )


def save_optimizer(experiment_directory, filename, optimizer, epoch):

    optimizer_params_dir = ws.get_optimizer_params_dir(experiment_directory, True)

    torch.save(
        {"epoch": epoch, "optimizer_state_dict": optimizer.state_dict()},
        os.path.join(optimizer_params_dir, filename),
    )


def load_optimizer(experiment_directory, filename, optimizer):

    full_filename = os.path.join(
        ws.get_optimizer_params_dir(experiment_directory), filename
    )

    if not os.path.isfile(full_filename):
        raise Exception(
            'optimizer state dict "{}" does not exist'.format(full_filename)
        )

    data = torch.load(full_filename)

    optimizer.load_state_dict(data["optimizer_state_dict"])

    return data["epoch"]


def save_latent_vectors(experiment_directory, filename, latent_vec, epoch):

    latent_codes_dir = ws.get_latent_codes_dir(experiment_directory, True)

    all_latents = latent_vec.state_dict()

    torch.save(
        {"epoch": epoch, "latent_codes": all_latents},
        os.path.join(latent_codes_dir, filename),
    )


def load_latent_vectors(experiment_directory, filename, lat_vecs):

    full_filename = os.path.join(
        ws.get_latent_codes_dir(experiment_directory), filename
    )

    if not os.path.isfile(full_filename):
        raise Exception('latent state file "{}" does not exist'.format(full_filename))

    data = torch.load(full_filename)

    if isinstance(data["latent_codes"], torch.Tensor):

        # for backwards compatibility
        if not lat_vecs.num_embeddings == data["latent_codes"].size()[0]:
            raise Exception(
                "num latent codes mismatched: {} vs {}".format(
                    lat_vecs.num_embeddings, data["latent_codes"].size()[0]
                )
            )

        if not lat_vecs.embedding_dim == data["latent_codes"].size()[2]:
            raise Exception("latent code dimensionality mismatch")

        for i, lat_vec in enumerate(data["latent_codes"]):
            lat_vecs.weight.data[i, :] = lat_vec

    else:
        lat_vecs.load_state_dict(data["latent_codes"])

    return data["epoch"]


def save_logs(
    experiment_directory,
    loss_log,
    epoch,
):

    torch.save(
        {
            "epoch": epoch,
            "loss": loss_log,
        },
        os.path.join(experiment_directory, ws.logs_filename),
    )


def load_logs(experiment_directory):

    full_filename = os.path.join(experiment_directory, ws.logs_filename)

    if not os.path.isfile(full_filename):
        raise Exception('log file "{}" does not exist'.format(full_filename))

    data = torch.load(full_filename)

    return (
        data["loss"],
        data["epoch"],
    )


def clip_logs(loss_log, epoch):

    iters_per_epoch = len(loss_log) // len(lr_log)
    loss_log = loss_log[: (iters_per_epoch * epoch)]

    return loss_log


def get_spec_with_default(specs, key, default):
    try:
        return specs[key]
    except KeyError:
        return default


def get_mean_latent_vector_magnitude(latent_vectors):
    return torch.mean(torch.norm(latent_vectors.weight.data.detach(), dim=1))


def append_parameter_magnitudes(param_mag_log, model):
    for name, param in model.named_parameters():
        if len(name) > 7 and name[:7] == "module.":
            name = name[7:]
        if name not in param_mag_log.keys():
            param_mag_log[name] = []
        param_mag_log[name].append(param.data.norm().item())


def fourier_transform(x, L=5):
    cosines = torch.cat([torch.cos(2**l*3.1415*x) for l in range(L)], -1)
    sines = torch.cat([torch.sin(2**l*3.1415*x) for l in range(L)], -1)
    transformed_x = torch.cat((cosines,sines),-1)
    return transformed_x


def add_common_args(arg_parser):
    arg_parser.add_argument(
        "--debug",
        dest="debug",
        default=False,
        action="store_true",
        help="If set, debugging messages will be printed",
    )
    arg_parser.add_argument(
        "--quiet",
        "-q",
        dest="quiet",
        default=False,
        action="store_true",
        help="If set, only warnings will be printed",
    )
    arg_parser.add_argument(
        "--log",
        dest="logfile",
        default=None,
        help="If set, the log will be saved using the specified filename.",
    )


def configure_logging(args):
    logger = logging.getLogger()
    if args.debug:
        logger.setLevel(logging.DEBUG)
    elif args.quiet:
        logger.setLevel(logging.WARNING)
    else:
        logger.setLevel(logging.INFO)
    logger_handler = logging.StreamHandler()
    formatter = logging.Formatter("MeshSdf - %(levelname)s - %(message)s")
    logger_handler.setFormatter(formatter)
    logger.addHandler(logger_handler)

    if args.logfile is not None:
        file_logger_handler = logging.FileHandler(args.logfile)
        file_logger_handler.setFormatter(formatter)
        logger.addHandler(file_logger_handler)


def decode_sdf(decoder, latent_vector, queries):
    num_samples = queries.shape[0]
    latent_repeat = latent_vector.expand(num_samples, -1)
    sdf = decoder(latent_repeat, queries)
    return sdf



def get_projection(az, el, distance, focal_length=35, img_w=256, img_h=256, sensor_size_mm = 32.):
    """Calculate 4x3 3D to 2D projection matrix given viewpoint parameters."""

    # Calculate intrinsic matrix.
    f_u = focal_length * img_w  / sensor_size_mm
    f_v = focal_length * img_h  / sensor_size_mm
    u_0 = img_w / 2
    v_0 = img_h / 2
    K = np.matrix(((f_u, 0, u_0), (0, f_v, v_0), (0, 0, 1)))

    # Calculate rotation and translation matrices.
    sa = np.sin(np.radians(az))
    ca = np.cos(np.radians(az))
    R_azimuth = np.transpose(np.matrix(((ca, 0, sa),
                                          (0, 1, 0),
                                          (-sa, 0, ca))))
    se = np.sin(np.radians(el))
    ce = np.cos(np.radians(el))
    R_elevation = np.transpose(np.matrix(((1, 0, 0),
                                          (0, ce, -se),
                                          (0, se, ce))))
    # fix up camera
    se = np.sin(np.radians(90))
    ce = np.cos(np.radians(90))
    R_cam = np.transpose(np.matrix(((ce, -se, 0),
                                          (se, ce, 0),
                                          (0, 0, 1))))
    T_world2cam = np.transpose(np.matrix((0,
                                           0,
                                           distance)))
    RT = np.hstack((R_cam@R_elevation@R_azimuth, T_world2cam))

    return K, RT
