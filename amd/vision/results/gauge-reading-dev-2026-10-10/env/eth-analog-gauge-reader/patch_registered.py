"""Opt-in registered-instrument mode (env ETH_REGISTERED_JSON = {image file name: {min, max, unit}}): map the
start/end notch angles to the registered scale min/max instead of fitting OCR'd numbers. Models unchanged.
Works with LF or CRLF checkouts."""
p = "pipeline.py"
t = open(p, newline="").read()
nl = "\r\n" if "\r\n" in t else "\n"
anchor = "    # ------------------Project OCR Numbers to ellipse-------------------------"
assert t.count(anchor) == 1, "anchor"
block = [
    "    reg = REGISTERED.get(os.path.basename(os.path.normpath(run_path)))",
    "    if reg is not None:",
    "        if not (start_point.shape == (1, 2) and end_point.shape == (1, 2)):",
    "            result.append({constants.READING_KEY: constants.FAILED})",
    "            write_files(result, result_full, errors, run_path, eval_mode)",
    "            raise Exception('Registered mode: start/end notch not detected')",
    "        point_needle_ellipse = get_line_ellipse_point(",
    "            needle_line_coeffs, (needle_line_start_x, needle_line_end_x), ellipse_params)",
    "        if point_needle_ellipse.shape[0] == 0:",
    "            result.append({constants.READING_KEY: constants.FAILED})",
    "            write_files(result, result_full, errors, run_path, eval_mode)",
    "            raise Exception('Needle line and ellipse do not intersect')",
    "        conv = AngleConverter(theta_zero)",
    "        t_s = conv.convert_angle(get_polar_angle(start_point.flatten(), ellipse_params))",
    "        t_e = conv.convert_angle(get_polar_angle(end_point.flatten(), ellipse_params))",
    "        t_n = conv.convert_angle(get_polar_angle(point_needle_ellipse, ellipse_params))",
    "        reading = reg['min'] + (t_n - t_s) / (t_e - t_s) * (reg['max'] - reg['min'])",
    "        errors['Registered mode'] = {'theta_start': float(t_s), 'theta_end': float(t_e), 'theta_needle': float(t_n)}",
    "        result.append({constants.READING_KEY: float(reading), constants.MEASURE_UNIT_KEY: reg['unit']})",
    "        write_files(result, result_full, errors, run_path, eval_mode)",
    "        return {'value': float(reading), 'unit': reg['unit']}",
    "",
    "",
]
t = t.replace(anchor, nl.join(block) + anchor)
imp = "import json" + nl
assert t.count(imp) == 1, "import json"
loader = ("REGISTERED = json.load(open(os.environ['ETH_REGISTERED_JSON'])) "
          "if os.environ.get('ETH_REGISTERED_JSON') else {}")
t = t.replace(imp, imp + loader + nl, 1)
open(p, "w", newline="").write(t)
print("patched registered mode")
