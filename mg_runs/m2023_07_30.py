import argparse

import jax
import jax.numpy as jnp

import x_xy
from neural_networks.logging import Logger, NeptuneLogger
from neural_networks.rnno import dustin_exp_xml, rnno_v2, train
from neural_networks.rnno.train import DebugInfo
from neural_networks.rnno.training_loop import TrainingLoopCallback

three_seg_seg2 = r"""
<x_xy model="three_seg_seg2">
    <options gravity="0 0 9.81" dt="0.01"/>
    <worldbody>
        <body name="seg2" joint="free">
            <body name="seg1" joint="ry">
                <body name="imu1" joint="frozen"/>
            </body>
            <body name="seg3" joint="rz">
                <body name="imu2" joint="frozen"/>
            </body>
        </body>
    </worldbody>
</x_xy>
"""


def draw_pos_uniform(key, pos_min, pos_max):
    key, c1, c2, c3 = jax.random.split(key, num=4)
    pos = jnp.array(
        [
            jax.random.uniform(c1, minval=pos_min[0], maxval=pos_max[0]),
            jax.random.uniform(c2, minval=pos_min[1], maxval=pos_max[1]),
            jax.random.uniform(c3, minval=pos_min[2], maxval=pos_max[2]),
        ]
    )
    return key, pos


def setup_fn_seg2(key, sys: x_xy.base.System) -> x_xy.base.System:
    def replace_pos(transforms, new_pos, name: str):
        i = sys.name_to_idx(name)
        return transforms.index_set(i, transforms[i].replace(pos=new_pos))

    ts = sys.links.transform1

    # seg1 relative to seg2
    key, pos = draw_pos_uniform(key, [-0.3, -0.02, -0.02], [-0.05, 0.02, 0.02])
    ts = replace_pos(ts, pos, "seg1")

    # imu1 relative to seg1
    key, pos = draw_pos_uniform(key, [-0.25, -0.05, -0.05], [-0.05, 0.05, 0.05])
    ts = replace_pos(ts, pos, "imu1")

    # seg3 relative to seg2
    key, pos = draw_pos_uniform(key, [0.05, -0.02, -0.02], [0.3, 0.02, 0.02])
    ts = replace_pos(ts, pos, "seg3")

    # imu2 relative to seg2
    key, pos = draw_pos_uniform(key, [0.05, -0.05, -0.05], [0.25, 0.05, 0.05])
    ts = replace_pos(ts, pos, "imu2")

    return sys.replace(links=sys.links.replace(transform1=ts))


def finalize_fn_imu_data(key, q, x, sys):
    imu_seg_attachment = {"imu1": "seg1", "imu2": "seg3"}

    X = {}
    for imu, seg in imu_seg_attachment.items():
        key, consume = jax.random.split(key)
        X[seg] = x_xy.algorithms.imu(x.take(sys.name_to_idx(imu), 1), sys.gravity, sys.dt, consume, True)
    return X


def finalize_fn_rel_pose_data(key, _, x, sys):
    dustin_sys = x_xy.io.load_sys_from_str(dustin_exp_xml)
    y = x_xy.algorithms.rel_pose(dustin_sys, x, sys)
    return y


def finalize_fn(key, q, x, sys):
    X = finalize_fn_imu_data(key, q, x, sys)
    y = finalize_fn_rel_pose_data(key, q, x, sys)
    return X, y


class LogLossWeightMetrics(TrainingLoopCallback):
    def after_training_step(
        self,
        i_episode,
        metrices,
        params,
        debug_info: DebugInfo,
        sample_eval,
        loggers: list[Logger],
    ) -> None:
        for logger in loggers:
            if isinstance(logger, NeptuneLogger):
                for perc, vals in debug_info.top_n.items():
                    mean = jnp.mean(vals[-1])
                    std = jnp.std(vals[-1])

                    logger.run[f"loss_top_n/top{perc}_mean"].append(mean)
                    logger.run[f"loss_top_n/top{perc}_std"].append(std)


def run(batch_size: int, beta: float | None = None, *, iterations: int = 1500, seed: int = 0, debug: bool = False):
    sys = x_xy.io.load_sys_from_str(three_seg_seg2)
    config = x_xy.algorithms.RCMG_Config(t_min=0.05, t_max=0.3, dang_min=0.1, dang_max=3.0, dpos_max=0.3)
    gen = x_xy.algorithms.build_generator(sys, config, setup_fn_seg2, finalize_fn)
    gen = x_xy.algorithms.batch_generator(gen, batch_size)

    rnno = rnno_v2(x_xy.io.load_sys_from_str(dustin_exp_xml))

    key = jax.random.PRNGKey(seed)

    key_network, key_generator = jax.random.split(key)

    loggers = []

    if not debug:
        neptune_logger = NeptuneLogger()

        neptune_logger.run["sys/tags"].add(["softmax_2"])
        neptune_logger.run["seed"] = seed

        neptune_logger.run["beta"] = beta or "None"

        loggers.append(neptune_logger)

    debug_info = train(
        gen,
        iterations,
        rnno,
        loggers=loggers,
        key_network=key_network,
        key_generator=key_generator,
        beta=beta,
        callbacks=[LogLossWeightMetrics()],
    )

    return debug_info


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument("--batchsize", type=int, required=True, dest="batch_size")

    parser.add_argument("--beta", type=float)

    parser.add_argument("--seed", type=int)

    parser.add_argument("--iterations", type=int)

    parser.add_argument("--debug", action="store_true", default=False)

    args = parser.parse_args()

    if args.debug:
        print(f"{args=}")

    args = {key: val for key, val in vars(args).items() if val is not None}

    debug_info = run(**args)
