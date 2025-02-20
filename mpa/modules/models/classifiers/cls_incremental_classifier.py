from mmcls.models.builder import CLASSIFIERS
from mmcls.models.classifiers.image import ImageClassifier


@CLASSIFIERS.register_module()
class ClsIncrementalClassifier(ImageClassifier):

    def __init__(self, backbone, neck=None, head=None, pretrained=None):
        super(ClsIncrementalClassifier, self).__init__(
            backbone, neck=neck, head=head, pretrained=pretrained
        )

    def forward_train(self, img, gt_label, **kwargs):
        soft_label = kwargs.pop('soft_label', None)
        center = kwargs.pop('center', None)
        x = self.extract_feat(img)
        losses = dict()
        loss = self.head.forward_train(x, gt_label, soft_label, center)
        losses.update(loss)
        return losses

    def extract_prob(self, img):
        """Test without augmentation."""
        x = self.extract_feat(img)
        return self.head.extract_prob(x), x
