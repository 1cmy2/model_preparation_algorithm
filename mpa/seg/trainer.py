import os
import time
import numbers
import glob
import torch
import torch.multiprocessing as mp
import torch.distributed as dist

import mmcv
from mmcv import get_git_hash

from mmseg import __version__
# from mmseg.apis import train_segmentor
from .train import train_segmentor
# from mmseg.datasets import build_dataset
from .builder import build_dataset
from mmseg.models import build_segmentor
from mmseg.utils import collect_env

from mpa.registry import STAGES
from mpa.seg.stage import SegStage
from mpa.utils.logger import get_logger

logger = get_logger()


@STAGES.register_module()
class SegTrainer(SegStage):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def run(self, model_cfg, model_ckpt, data_cfg, **kwargs):
        """Run training stage for segmentation

        - Configuration
        - Environment setup
        - Run training via MMSegmentation -> MMCV
        """
        self._init_logger()
        mode = kwargs.get('mode', 'train')
        if mode not in self.mode:
            return {}

        cfg = self.configure(model_cfg, model_ckpt, data_cfg, **kwargs)

        if cfg.runner.type == 'IterBasedRunner':
            cfg.runner = dict(type=cfg.runner.type, max_iters=cfg.runner.max_iters)

        logger.info('train!')

        # Work directory
        mmcv.mkdir_or_exist(os.path.abspath(cfg.work_dir))

        timestamp = time.strftime('%Y%m%d_%H%M%S', time.localtime())

        # Environment
        distributed = False
        if cfg.gpu_ids is not None:
            if isinstance(cfg.get('gpu_ids'), numbers.Number):
                cfg.gpu_ids = [cfg.get('gpu_ids')]
            if len(cfg.gpu_ids) > 1:
                distributed = True
        logger.info(f'cfg.gpu_ids = {cfg.gpu_ids}, distributed = {distributed}')
        env_info_dict = collect_env()
        env_info = '\n'.join([(f'{k}: {v}') for k, v in env_info_dict.items()])
        dash_line = '-' * 60 + '\n'
        logger.info('Environment info:\n' + dash_line + env_info + '\n' + dash_line)

        # Data
        datasets = [build_dataset(cfg.data.train)]

        # Dataset for HPO
        hp_config = kwargs.get('hp_config', None)
        if hp_config is not None:
            import hpopt

            if isinstance(datasets[0], list):
                for idx, ds in enumerate(datasets[0]):
                    datasets[0][idx] = hpopt.createHpoDataset(ds, hp_config)
            else:
                datasets[0] = hpopt.createHpoDataset(datasets[0], hp_config)

        # Target classes
        if 'task_adapt' in cfg:
            target_classes = cfg.task_adapt.final
        else:
            target_classes = datasets[0].CLASSES

        # Metadata
        meta = dict()
        meta['env_info'] = env_info
        # meta['config'] = cfg.pretty_text
        meta['seed'] = cfg.seed
        meta['exp_name'] = cfg.work_dir
        if cfg.checkpoint_config is not None:
            cfg.checkpoint_config.meta = dict(
                mmseg_version=__version__ + get_git_hash()[:7],
                CLASSES=target_classes)

        if distributed:
            if cfg.dist_params.get('linear_scale_lr', False):
                new_lr = len(cfg.gpu_ids) * cfg.optimizer.lr
                logger.info(f'enabled linear scaling rule to the learning rate. \
                    changed LR from {cfg.optimizer.lr} to {new_lr}')
                cfg.optimizer.lr = new_lr

        # Save config
        # cfg.dump(os.path.join(cfg.work_dir, 'config.yaml'))
        # logger.info(f'Config:\n{cfg.pretty_text}')

        if distributed:
            os.environ['MASTER_ADDR'] = cfg.dist_params.get('master_addr', 'localhost')
            os.environ['MASTER_PORT'] = cfg.dist_params.get('master_port', '29500')
            mp.spawn(SegTrainer.train_worker, nprocs=len(cfg.gpu_ids),
                     args=(target_classes, datasets, cfg, distributed, True, timestamp, meta))
        else:
            SegTrainer.train_worker(
                None,
                target_classes,
                datasets,
                cfg,
                distributed,
                True,
                timestamp,
                meta
            )

        # Save outputs
        output_ckpt_path = os.path.join(cfg.work_dir, 'latest.pth')
        best_ckpt_path = glob.glob(os.path.join(cfg.work_dir, 'best_mDice_*.pth'))
        if len(best_ckpt_path) > 0:
            output_ckpt_path = best_ckpt_path[0]
        best_ckpt_path = glob.glob(os.path.join(cfg.work_dir, 'best_mIoU_*.pth'))
        if len(best_ckpt_path) > 0:
            output_ckpt_path = best_ckpt_path[0]
        return dict(final_ckpt=output_ckpt_path)

    @staticmethod
    def train_worker(gpu, target_classes, datasets, cfg, distributed=False, validate=False,
                     timestamp=None, meta=None):
        # logger = get_logger()
        if distributed:
            torch.cuda.set_device(gpu)
            dist.init_process_group(backend=cfg.dist_params.get('backend', 'nccl'),
                                    world_size=len(cfg.gpu_ids), rank=gpu)
            logger.info(f'dist info world_size = {dist.get_world_size()}, rank = {dist.get_rank()}')

        # Model
        model = build_segmentor(cfg.model)
        model.CLASSES = target_classes

        train_segmentor(
            model,
            datasets,
            cfg,
            distributed=distributed,
            validate=True,
            timestamp=timestamp,
            meta=meta
        )
