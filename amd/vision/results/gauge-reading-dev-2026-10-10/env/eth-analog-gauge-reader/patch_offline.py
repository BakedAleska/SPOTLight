"""Load DINOv2 from the pinned local hub copy (no GitHub call per image). Model and weights unchanged."""
p = "key_point_detection/model.py"
t = open(p, newline="").read()
a, b = "torch.hub.load('facebookresearch/dinov2',", "torch.hub.load('/root/.cache/torch/hub/facebookresearch_dinov2_main',"
c, d = "pretrained=pretrained)", "pretrained=pretrained, source='local')"
assert t.count(a) == 1 and t.count(c) == 1, (t.count(a), t.count(c))
open(p, "w", newline="").write(t.replace(a, b).replace(c, d))
print("patched", p)
