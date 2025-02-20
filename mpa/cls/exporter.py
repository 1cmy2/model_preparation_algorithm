import os
import torch.onnx
from functools import partial

from mmcv.runner import load_checkpoint, wrap_fp16_model

from mmcls.models import build_classifier
from mpa.registry import STAGES
from .stage import ClsStage
from mpa.utils import mo_wrapper
from mpa.utils.logger import get_logger
import numpy as np
import torch
from mmcls.datasets.pipelines import Compose
logger = get_logger()


@STAGES.register_module()
class ClsExporter(ClsStage):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def get_fake_input(self, cfg, orig_img_shape=(128, 128, 3)):
        pipeline = cfg.data.test.pipeline
        pipeline = Compose(pipeline)
        data = dict(img=np.zeros(orig_img_shape, dtype=np.uint8))
        data = pipeline(data)
        return data

    def get_norm_values(self, cfg):
        pipeline = cfg.data.test.pipeline
        mean_values = [0, 0, 0]
        scale_values = [1, 1, 1]
        for pipeline_step in pipeline:
            if pipeline_step.type == 'Normalize':
                mean_values = pipeline_step.mean
                scale_values = pipeline_step.std
                break
        return mean_values, scale_values

    def run(self, model_cfg, model_ckpt, data_cfg, **kwargs):
        """Run exporter stage

        """
        self._init_logger()
        mode = kwargs.get('mode', 'train')
        if mode not in self.mode:
            logger.warning(f'mode for this stage {mode}')
            return {}

        cfg = self.configure(model_cfg, model_ckpt, data_cfg, training=False, **kwargs)

        output_path = os.path.join(cfg.work_dir, 'export')
        onnx_path = output_path+'/model.onnx'
        os.makedirs(output_path, exist_ok=True)

        # build the model and load checkpoint
        model = build_classifier(cfg.model)
        fp16_cfg = cfg.get('fp16', None)
        if fp16_cfg is not None:
            wrap_fp16_model(model)
        logger.info('load checkpoint from ' + cfg.load_from)
        _ = load_checkpoint(model, cfg.load_from, map_location='cpu')
        if hasattr(model, 'is_export'):
            model.is_export = True
        model.eval()
        model.forward = partial(model.forward, img_metas={}, return_loss=False)

        data = self.get_fake_input(cfg)
        fake_img = data['img'].unsqueeze(0)

        try:
            torch.onnx.export(model,
                              fake_img,
                              onnx_path,
                              verbose=False,
                              export_params=True,
                              input_names=['data'],
                              output_names=['logits', 'features', 'vector'],
                              dynamic_axes={},
                              opset_version=11,
                              operator_export_type=torch.onnx.OperatorExportTypes.ONNX
                              )

            mean_values, scale_values = self.get_norm_values(cfg)
            mo_args = {
                'input_model': onnx_path,
                'mean_values': mean_values,
                'scale_values': scale_values,
                'data_type': 'FP32',
                'model_name': 'model',
                'reverse_input_channels': None,
            }

            ret, msg = mo_wrapper.generate_ir(output_path, output_path, silent=True, **mo_args)
            os.remove(onnx_path)

        except Exception as ex:
            return {'outputs': None, 'msg': f'exception {type(ex)}'}
        bin_file = [f for f in os.listdir(output_path) if f.endswith('.bin')][0]
        xml_file = [f for f in os.listdir(output_path) if f.endswith('.xml')][0]
        logger.info('Exporting completed')

        return {
            'outputs': {
                'bin': os.path.join(output_path, bin_file),
                'xml': os.path.join(output_path, xml_file)
            },
            'msg': ''
        }
