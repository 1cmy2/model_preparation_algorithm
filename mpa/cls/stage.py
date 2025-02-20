import copy
import torch
import numpy as np

from mmcv import ConfigDict
from mmcv import build_from_cfg

from mpa.stage import Stage
from mpa.utils.config_utils import update_or_add_custom_hook
from mpa.utils.logger import get_logger

logger = get_logger()

CLASS_INC_DATASET = ['MPAClsDataset', 'ClsDirDataset', 'ClsTVDataset']
PSEUDO_LABEL_ENABLE_DATASET = ['ClassIncDataset', 'LwfTaskIncDataset', 'ClsTVDataset']
WEIGHT_MIX_CLASSIFIER = ['SAMImageClassifier']


class ClsStage(Stage):
    def configure(self, model_cfg, model_ckpt, data_cfg, training=True, **kwargs):
        """Create MMCV-consumable config from given inputs
        """
        logger.info(f'configure: training={training}')

        # Recipe + model
        cfg = self.cfg
        if model_cfg:
            if hasattr(cfg, 'model'):
                cfg.merge_from_dict(model_cfg._cfg_dict)
            else:
                cfg.model = copy.deepcopy(model_cfg.model)

        if cfg.model.pop('task', None) != 'classification':
            raise ValueError(
                f'Given model_cfg ({model_cfg.filename}) is not supported by classification recipe'
            )
        self.configure_model(cfg, training, **kwargs)

        # Checkpoint
        if model_ckpt:
            cfg.load_from = self.get_model_ckpt(model_ckpt)

        # OMZ-plugin
        if cfg.model.backbone.type == 'OmzBackboneCls':
            ir_path = kwargs.get('ir_path', None)
            if ir_path is None:
                raise RuntimeError('OMZ model needs OpenVINO bin/XML files.')
            cfg.model.backbone.model_path = ir_path

        pretrained = kwargs.get('pretrained', None)
        if pretrained and isinstance(pretrained, str):
            logger.info(f'Overriding cfg.load_from -> {pretrained}')
            cfg.load_from = pretrained

        # Data
        if data_cfg:
            cfg.merge_from_dict(data_cfg)
        self.configure_data(cfg, training, **kwargs)

        # Task
        if 'task_adapt' in cfg:
            model_meta = self.get_model_meta(cfg)
            model_tasks, dst_classes = self.configure_task(cfg, training, model_meta, **kwargs)
            if model_tasks is not None:
                self.model_tasks = model_tasks
            if dst_classes is not None:
                self.model_classes = dst_classes
        else:
            if 'num_classes' not in cfg.data:
                cfg.data.num_classes = len(cfg.data.train.get('classes', []))
            cfg.model.head.num_classes = cfg.data.num_classes

        if isinstance(cfg.model.head.topk, tuple):
            cfg.model.head.topk = (1,) if cfg.model.head.num_classes < 5 else (1, 5)

        # Other hyper-parameters
        if cfg.get('hyperparams', False):
            self.configure_hyperparams(cfg, training, **kwargs)

        return cfg

    @staticmethod
    def configure_model(cfg, training, **kwargs):
        # verify and update model configurations
        # check whether in/out of the model layers require updating
        update_required = False
        if cfg.model.get('neck') is not None:
            if cfg.model.neck.get('in_channels') is not None and cfg.model.neck.in_channels <= 0:
                update_required = True
        if not update_required and cfg.model.get('head') is not None:
            if cfg.model.head.get('in_channels') is not None and cfg.model.head.in_channels <= 0:
                update_required = True
        if not update_required:
            return

        # update model layer's in/out configuration
        input_shape = [3, 224, 224]
        logger.debug(f'input shape for backbone {input_shape}')
        from mmcls.models.builder import BACKBONES as backbone_reg
        layer = build_from_cfg(cfg.model.backbone, backbone_reg)
        output = layer(torch.rand([1] + input_shape))
        if isinstance(output, (tuple, list)):
            output = output[-1]
        output = output.shape[1]
        if cfg.model.get('neck') is not None:
            if cfg.model.neck.get('in_channels') is not None:
                logger.info(f"'in_channels' config in model.neck is updated from "
                            f"{cfg.model.neck.in_channels} to {output}")
                cfg.model.neck.in_channels = output
                input_shape = [i for i in range(output)]
                logger.debug(f'input shape for neck {input_shape}')
                from mmcls.models.builder import NECKS as neck_reg
                layer = build_from_cfg(cfg.model.neck, neck_reg)
                output = layer(torch.rand([1] + input_shape))
                if isinstance(output, (tuple, list)):
                    output = output[-1]
                output = output.shape[1]
        if cfg.model.get('head') is not None:
            if cfg.model.head.get('in_channels') is not None:
                logger.info(f"'in_channels' config in model.head is updated from "
                            f"{cfg.model.head.in_channels} to {output}")
                cfg.model.head.in_channels = output

            # checking task incremental model configurations

    @staticmethod
    def configure_task(cfg, training, model_meta=None, **kwargs):
        """Configure for Task Adaptation Task
        """
        task_adapt_type = cfg['task_adapt'].get('type', None)
        adapt_type = cfg['task_adapt'].get('op', 'REPLACE')

        model_tasks, dst_classes = None, None
        model_classes, data_classes = [], []
        train_data_cfg = Stage.get_train_data_cfg(cfg)
        if isinstance(train_data_cfg, list):
            train_data_cfg = train_data_cfg[0]

        model_classes = Stage.get_model_classes(cfg)
        data_classes = Stage.get_data_classes(cfg)
        if model_classes:
            cfg.model.head.num_classes = len(model_classes)
            model_meta['CLASSES'] = model_classes
        elif data_classes:
            cfg.model.head.num_classes = len(data_classes)
            model_meta['CLASSES'] = data_classes

        if not train_data_cfg.get('new_classes', False):  # when train_data_cfg doesn't have 'new_classes' key
            new_classes = np.setdiff1d(data_classes, model_classes).tolist()
            train_data_cfg['new_classes'] = new_classes

        if training:
            # if Trainer to Stage configure, training = True
            if train_data_cfg.get('tasks'):
                # Task Adaptation
                if model_meta.get('tasks', False):
                    model_tasks, old_tasks = refine_tasks(train_data_cfg, model_meta, adapt_type)
                else:
                    raise KeyError(f'can not find task meta data from {cfg.load_from}.')
                cfg.model.head.update({'old_tasks': old_tasks})
                # update model.head.tasks with training dataset's tasks if it's configured as None
                if cfg.model.head.get('tasks') is None:
                    logger.info("'tasks' in model.head is None. updated with configuration on train data "
                                f"{train_data_cfg.get('tasks')}")
                    cfg.model.head.update({'tasks': train_data_cfg.get('tasks')})
            elif 'new_classes' in train_data_cfg:
                # Class-Incremental
                if model_meta.get('CLASSES', False):
                    dst_classes, old_classes = refine_cls(train_data_cfg, model_meta, adapt_type)
                else:
                    raise KeyError(f'can not find CLASSES or classes meta data from {cfg.load_from}.')
            else:
                raise KeyError(
                    '"new_classes" or "tasks" should be defined for incremental learning w/ current model.'
                )

            if task_adapt_type == 'mpa':
                if train_data_cfg.type not in CLASS_INC_DATASET:  # task incremental is not supported yet
                    raise NotImplementedError(
                        f'Class Incremental Learning for {train_data_cfg.type} is not yet supported!')

                if cfg.model.type in WEIGHT_MIX_CLASSIFIER:
                    cfg.model.task_adapt = ConfigDict(
                        src_classes=model_classes,
                        dst_classes=data_classes,
                    )

                # Train dataset config update
                train_data_cfg.new_classes = np.setdiff1d(dst_classes, old_classes).tolist()
                train_data_cfg.classes = dst_classes

                # model configuration update
                cfg.model.head.num_classes = len(dst_classes)
                gamma = 2 if cfg['task_adapt'].get('efficient_mode', False) else 3
                cfg.model.head.loss = ConfigDict(
                    type='SoftmaxFocalLoss',
                    loss_weight=1.0,
                    gamma=gamma,
                    reduction='none',
                )
                # if op='REPLACE' & no new_classes (REMOVE), then sampler_flag = False
                sampler_flag = True if len(train_data_cfg.new_classes) > 0 else False

                # Update Task Adapt Hook
                task_adapt_hook = ConfigDict(
                    type='TaskAdaptHook',
                    src_classes=old_classes,
                    dst_classes=dst_classes,
                    model_type=cfg.model.type,
                    sampler_flag=sampler_flag,
                    efficient_mode=cfg['task_adapt'].get('efficient_mode', False)
                )
                update_or_add_custom_hook(cfg, task_adapt_hook)

        else:  # if not training phase (eval)
            if train_data_cfg.get('tasks'):
                if model_meta.get('tasks', False):
                    cfg.model.head['tasks'] = model_meta['tasks']
                else:
                    raise KeyError(f'can not find task meta data from {cfg.load_from}.')
            elif train_data_cfg.get('new_classes'):
                if model_meta.get('CLASSES', False):
                    dst_classes, _ = refine_cls(train_data_cfg, model_meta, adapt_type)
                    cfg.model.head.num_classes = len(dst_classes)
                else:
                    raise KeyError(f'can not find classes meta data from {cfg.load_from}.')

        # Pseudo label augmentation
        pre_stage_res = kwargs.get('pre_stage_res', None)
        if pre_stage_res:
            logger.info(f'pre-stage dataset: {pre_stage_res}')
            if train_data_cfg.type not in PSEUDO_LABEL_ENABLE_DATASET:
                raise NotImplementedError(
                    f'Pseudo label loading for {train_data_cfg.type} is not yet supported!')
            train_data_cfg.pre_stage_res = pre_stage_res
            if train_data_cfg.get('tasks'):
                train_data_cfg.model_tasks = model_tasks
                cfg.model.head.old_tasks = old_tasks
            elif train_data_cfg.get('CLASSES'):
                train_data_cfg.dst_classes = dst_classes
                cfg.data.val.dst_classes = dst_classes
                cfg.data.test.dst_classes = dst_classes
                cfg.model.head.num_classes = len(train_data_cfg.dst_classes)
                cfg.model.head.num_old_classes = len(old_classes)
        return model_tasks, dst_classes

    @staticmethod
    def configure_hyperparams(cfg, training, **kwargs):
        hyperparams = kwargs.get('hyperparams', None)
        if hyperparams is not None:
            bs = hyperparams.get('bs', None)
            if bs is not None:
                cfg.data.samples_per_gpu = bs

            lr = hyperparams.get('lr', None)
            if lr is not None:
                cfg.optimizer.lr = lr


def refine_tasks(train_cfg, meta, adapt_type):
    new_tasks = train_cfg['tasks']
    if adapt_type == 'REPLACE':
        old_tasks = {}
        model_tasks = new_tasks
    elif adapt_type == 'MERGE':
        old_tasks = meta['tasks']
        model_tasks = copy.deepcopy(old_tasks)
        for task, cls in new_tasks.items():
            if model_tasks.get(task):
                model_tasks[task] = model_tasks[task] \
                                            + [c for c in cls if c not in model_tasks[task]]
            else:
                model_tasks.update({task: cls})
    else:
        raise KeyError(f'{adapt_type} is not supported for task_adapt options!')
    return model_tasks, old_tasks


def refine_cls(train_cfg, meta, adapt_type):
    # Get 'new_classes' in data.train_cfg & get 'old_classes' pretreained model meta data CLASSES
    new_classes = train_cfg['new_classes']
    old_classes = meta['CLASSES']
    if adapt_type == 'REPLACE':
        # if 'REPLACE' operation, then old_classes -> new_classes
        if len(new_classes) == 0:
            raise ValueError('Data classes should contain at least one class!')
        dst_classes = new_classes.copy()
    elif adapt_type == 'MERGE':
        # if 'MERGE' operation, then old_classes -> old_classes + new_classes (merge)
        dst_classes = old_classes + [cls for cls in new_classes if cls not in old_classes]
    else:
        raise KeyError(f'{adapt_type} is not supported for task_adapt options!')
    return dst_classes, old_classes
