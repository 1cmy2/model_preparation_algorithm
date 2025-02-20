import functools
from mmdet.models.builder import DETECTORS
from mmdet.models.detectors.single_stage import SingleStageDetector
from .sam_detector_mixin import SAMDetectorMixin
from .l2sp_detector_mixin import L2SPDetectorMixin
from mpa.modules.utils.task_adapt import map_class_names
from mpa.utils.logger import get_logger

logger = get_logger()


@DETECTORS.register_module()
class CustomSingleStageDetector(SAMDetectorMixin, L2SPDetectorMixin, SingleStageDetector):
    """SAM optimizer & L2SP regularizer enabled custom SSD
    """
    def __init__(self, *args, task_adapt=None, **kwargs):
        super().__init__(*args, **kwargs)

        # Hook for class-sensitive weight loading
        if task_adapt:
            self._register_load_state_dict_pre_hook(
                functools.partial(
                    self.load_state_dict_pre_hook,
                    self,  # model
                    task_adapt['dst_classes'],  # model_classes
                    task_adapt['src_classes']   # chkpt_classes
                )
            )

    def forward_train(self,
                      img,
                      img_metas,
                      gt_bboxes,
                      gt_labels,
                      gt_bboxes_ignore=None,
                      **kwargs):
        """
        Args:
            img (Tensor): Input images of shape (N, C, H, W).
                Typically these should be mean centered and std scaled.
            img_metas (list[dict]): A List of image info dict where each dict
                has: 'img_shape', 'scale_factor', 'flip', and may also contain
                'filename', 'ori_shape', 'pad_shape', and 'img_norm_cfg'.
                For details on the values of these keys see
                :class:`mmdet.datasets.pipelines.Collect`.
            gt_bboxes (list[Tensor]): Each item are the truth boxes for each
                image in [tl_x, tl_y, br_x, br_y] format.
            gt_labels (list[Tensor]): Class indices corresponding to each box
            gt_bboxes_ignore (None | list[Tensor]): Specify which bounding
                boxes can be ignored when computing the loss.

        Returns:
            dict[str, Tensor]: A dictionary of loss components.
        """
        batch_input_shape = tuple(img[0].size()[-2:])
        for img_meta in img_metas:
            img_meta['batch_input_shape'] = batch_input_shape
        x = self.extract_feat(img)
        losses = self.bbox_head.forward_train(x, img_metas, gt_bboxes,
                                              gt_labels, gt_bboxes_ignore, **kwargs)
        return losses

    @staticmethod
    def load_state_dict_pre_hook(model, model_classes, chkpt_classes, chkpt_dict, prefix, *args, **kwargs):
        """Modify input state_dict according to class name matching before weight loading
        """
        logger.info(f'----------------- CustomSSD.load_state_dict_pre_hook() called w/ prefix: {prefix}')

        # Dst to chkpt mapping index (including BG class)
        model_dict = model.state_dict()
        chkpt_classes = list(chkpt_classes) + ['__BG__']
        model_classes = list(model_classes) + ['__BG__']
        num_chkpt_classes = len(chkpt_classes)
        num_model_classes = len(model_classes)
        model2chkpt = map_class_names(model_classes, chkpt_classes)
        logger.info(f'{chkpt_classes} -> {model_classes}')

        # List of class-relevant params
        if prefix + 'bbox_head.cls_convs.0.weight' in chkpt_dict:
            param_names = [
                'bbox_head.cls_convs.{}.weight',  # normal
                'bbox_head.cls_convs.{}.bias',
            ]
        elif prefix + 'bbox_head.cls_convs.0.0.weight' in chkpt_dict:
            param_names = [
                'bbox_head.cls_convs.{}.3.weight',  # depth-wise: (0)conv -> (1)bn -> (2)act -> (3)conv
                'bbox_head.cls_convs.{}.3.bias',
            ]
        else:
            param_names = []

        # Weight mixing
        for level in range(10):  # For each level (safer inspection loop than 'while True'. Mostly has 2~3 levels)
            level_found = False
            for model_name in param_names:
                model_name = model_name.format(level)
                chkpt_name = prefix + model_name
                if model_name not in model_dict or chkpt_name not in chkpt_dict:
                    logger.info(f'Skipping weight copy: {chkpt_name}')
                    break
                level_found = True

                model_param = model_dict[model_name].clone()
                chkpt_param = chkpt_dict[chkpt_name]

                num_chkpt_anchors = int(chkpt_param.shape[0] / num_chkpt_classes)
                num_model_anchors = int(model_param.shape[0] / num_model_classes)
                num_anchors = min(num_chkpt_anchors, num_model_anchors)
                logger.info(f'Mixing {model_name}: {num_chkpt_anchors}x{num_chkpt_classes} -> '
                            f'{num_model_anchors}x{num_model_classes} anchors')

                for anchor_idx in range(num_anchors):  # For each anchor
                    for m, c in enumerate(model2chkpt):
                        if c >= 0:
                            # Copying only matched weight rows
                            model_param[anchor_idx * num_model_classes + m].copy_(
                                chkpt_param[anchor_idx * num_chkpt_classes + c])

                # Replace checkpoint weight by mixed weights
                chkpt_dict[chkpt_name] = model_param
            if not level_found:
                break
