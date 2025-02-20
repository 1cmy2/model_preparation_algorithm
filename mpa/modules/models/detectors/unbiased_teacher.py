import torch
import numpy as np
import copy
import functools
from collections import OrderedDict

from mmdet.models import DETECTORS, build_detector
from mmdet.models.detectors import BaseDetector
from .sam_detector_mixin import SAMDetectorMixin
from mpa.utils.logger import get_logger

logger = get_logger()


@DETECTORS.register_module()
class UnbiasedTeacher(SAMDetectorMixin, BaseDetector):
    """Unbiased teacher frameowork for general detectors
    """

    def __init__(
        self,
        unlabeled_loss_weight=1.0,
        unlabeled_loss_names=['loss_cls', ],
        pseudo_conf_thresh=0.7,
        enable_unlabeled_loss=False,
        bg_loss_weight=-1.0,
        **kwargs
    ):
        super().__init__()
        self.unlabeled_loss_weight = unlabeled_loss_weight
        self.unlabeled_loss_names = unlabeled_loss_names
        self.pseudo_conf_thresh = pseudo_conf_thresh
        self.unlabeled_loss_enabled = enable_unlabeled_loss
        self.bg_loss_weight = bg_loss_weight

        cfg = kwargs.copy()
        arch_type = cfg.pop('arch_type')
        cfg['type'] = arch_type
        self.model_s = build_detector(cfg)
        self.model_t = copy.deepcopy(self.model_s)

        # Hooks for super_type transparent weight load/save
        self._register_state_dict_hook(self.state_dict_hook)
        self._register_load_state_dict_pre_hook(
            functools.partial(self.load_state_dict_pre_hook, self)
        )

    def extract_feat(self, imgs):
        return self.model_t.extract_feat(imgs)

    def simple_test(self, img, img_metas, **kwargs):
        return self.model_t.simple_test(img, img_metas, **kwargs)

    def aug_test(self, imgs, img_metas, **kwargs):
        return self.model_t.aug_test(imgs, img_metas, **kwargs)

    def forward_dummy(self, img, **kwargs):
        return self.model_t.forward_dummy(img, **kwargs)

    def enable_unlabeled_loss(self, mode=True):
        self.unlabeled_loss_enabled = mode

    def forward_train(self,
                      img,
                      img_metas,
                      img0,
                      gt_bboxes,
                      gt_labels,
                      gt_bboxes_ignore=None,
                      **kwargs):

        losses = {}

        # Supervised loss
        # TODO: check img0 only option (which is common for mean teacher method)
        sl_losses = self.model_s.forward_train(
            torch.cat((img0, img)),  # weak + hard augmented images
            img_metas + img_metas,
            gt_bboxes + gt_bboxes,
            gt_labels + gt_labels,
            gt_bboxes_ignore + gt_bboxes_ignore if gt_bboxes_ignore else None
        )
        losses.update(sl_losses)

        # Pseudo labels from teacher
        ul_args = kwargs.get('extra_0', {})  # Supposing ComposedDL([labeled, unlabeled]) data loader
        ul_img = ul_args.get('img')
        ul_img0 = ul_args.get('img0')
        ul_img_metas = ul_args.get('img_metas')
        if ul_img is None:
            return losses
        with torch.no_grad():
            teacher_outputs = self.model_t.forward_test(
                [ul_img0],  # easy augmentation
                [ul_img_metas],
                rescale=False,
                postprocess=False
            )
        pseudo_bboxes, pseudo_labels, pseudo_ratio = self.generate_pseudo_labels(teacher_outputs, **kwargs)
        ps_recall = self.eval_pseudo_label_recall(pseudo_bboxes, ul_args.get('gt_bboxes', []))
        losses.update(ps_recall=ps_recall)
        losses.update(ps_ratio=torch.Tensor([pseudo_ratio]))

        if not self.unlabeled_loss_enabled or self.unlabeled_loss_weight <= 0.001:  # TODO: move back
            return losses

        # Unsupervised loss
        if self.bg_loss_weight >= 0.0:
            self.model_s.bbox_head.bg_loss_weight = self.bg_loss_weight
        ul_losses = self.model_s.forward_train(
            ul_img,  # hard augmentation
            ul_img_metas,
            pseudo_bboxes,
            pseudo_labels
        )
        if self.bg_loss_weight >= 0.0:
            self.model_s.bbox_head.bg_loss_weight = -1.0

        for ul_loss_name in self.unlabeled_loss_names:
            ul_loss = ul_losses[ul_loss_name]
            if isinstance(ul_loss, torch.Tensor):
                ul_loss = [ul_loss]
            losses[ul_loss_name + '_ul'] = [
                loss*self.unlabeled_loss_weight for loss in ul_loss
            ]
        # TODO: apply loss_bbox when adopting QFL;

        return losses

    def generate_pseudo_labels(self, teacher_outputs, **kwargs):
        all_pseudo_bboxes = []
        all_pseudo_labels = []
        num_all_bboxes = 0
        num_all_pseudo = 0
        if len(teacher_outputs[0][0].shape) == len(teacher_outputs[0][1].shape):
            teacher_outputs = [(teacher_outputs[0][i], teacher_outputs[1][i]) for i in range(len(teacher_outputs[0]))]
        for teacher_bboxes, teacher_labels in teacher_outputs:
            # print(teacher_bboxes.shape, teacher_labels.shape)
            confidences = teacher_bboxes[:, -1]
            pseudo_indices = confidences > self.pseudo_conf_thresh
            pseudo_bboxes = teacher_bboxes[pseudo_indices, :4]  # model output: [x y w h conf]
            pseudo_labels = teacher_labels[pseudo_indices]
            all_pseudo_bboxes.append(pseudo_bboxes)
            all_pseudo_labels.append(pseudo_labels)
            num_all_bboxes += teacher_bboxes.shape[0]
            num_all_pseudo += pseudo_bboxes.shape[0]
        # print(f'{num_all_pseudo} / {num_all_bboxes}')
        pseudo_ratio = float(num_all_pseudo)/num_all_bboxes if num_all_bboxes > 0 else 0.0
        return all_pseudo_bboxes, all_pseudo_labels, pseudo_ratio

    def eval_pseudo_label_recall(self, all_pseudo_bboxes, all_gt_bboxes):
        # For test only
        from mmdet.core.evaluation.recall import _recalls, bbox_overlaps
        img_num = len(all_gt_bboxes)
        if img_num == 0:
            return torch.Tensor([0.0])
        all_ious = np.ndarray((img_num,), dtype=object)
        for i in range(img_num):
            ps_bboxes = all_pseudo_bboxes[i]
            gt_bboxes = all_gt_bboxes[i]
            # prop_num = min(ps_bboxes.shape[0], 100)
            prop_num = ps_bboxes.shape[0]
            if gt_bboxes is None or gt_bboxes.shape[0] == 0:
                ious = np.zeros((0, ps_bboxes.shape[0]), dtype=np.float32)
            elif ps_bboxes is None or ps_bboxes.shape[0] == 0:
                ious = np.zeros((gt_bboxes.shape[0], 0), dtype=np.float32)
            else:
                ious = bbox_overlaps(
                    gt_bboxes.detach().cpu().numpy(),
                    ps_bboxes.detach().cpu().numpy()[:prop_num, :4]
                )
            all_ious[i] = ious
        recall = _recalls(all_ious, np.array([100]), np.array([0.5]))
        return torch.Tensor(recall)

    @staticmethod
    def state_dict_hook(module, state_dict, *args, **kwargs):
        """Redirect teacher model as output state_dict (student as auxilliary)
        """
        logger.info('----------------- UnbiasedTeacher.state_dict_hook() called')
        output = OrderedDict()
        for k, v in state_dict.items():
            if 'model_t.' in k:
                k = k.replace('model_t.', '')
            output[k] = v
        return output

    @staticmethod
    def load_state_dict_pre_hook(module, state_dict, *args, **kwargs):
        """Redirect input state_dict to teacher model
        """
        logger.info('----------------- UnbiasedTeacher.load_state_dict_pre_hook() called')
        for k in list(state_dict.keys()):
            v = state_dict.pop(k)
            if 'model_s.' not in k:
                k = 'model_t.' + k
            state_dict[k] = v
