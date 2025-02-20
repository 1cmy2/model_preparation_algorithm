from mmdet.models.detectors import BaseDetector


class SAMDetectorMixin(BaseDetector):
    """SAM-enabled detector mix-in
    """
    def train_step(self, data, optimizer, **kwargs):
        # Saving current batch data to compute SAM gradient
        # Rest of SAM logics are implented in SAMOptimizerHook
        self.current_batch = data
        return super().train_step(data, optimizer, **kwargs)
