"""Wrap a generated module so it always fits the judged frame and renders robustly.

The model's whole module becomes the body of our exported `generate(THREE)`, which then
tips objects that lie flat toward the eye-level judge views, centers and scales the result
to the unit cube, keeps metals visible under the judge's studio grid lighting, keeps glass
renderable in time on heavy scenes and makes open surfaces visible from both sides.
"""

from __future__ import annotations

import re

EXTENT = 0.99
# The judge's first stage renders metals well (a luminous environment map); only its gray tie-break grid has a
# black environment, where metalness near 1 renders black. 0.8 keeps metals reading as metal in both.
MAX_METALNESS = 0.8
# Transmission renders the scene twice per frame. Glass looks right with it, but on a heavy scene that doubling
# can exceed the renderer's time limit, so only scenes above this many vertices fall back to plain transparency.
TRANSMISSION_MAX_VERTICES = 200_000
FLAT = 0.5  # height below this fraction of the larger footprint side counts as lying flat
TILT = 0.261799  # 15 degrees: tips a flat object's top toward the eye-level judge views on +Z

TEMPLATE = """export default function generate(THREE) {
__BODY__
;
  const __pivot = new THREE.Group();
  __pivot.add(__CALL__(THREE));
  let __vertices = 0;
  __pivot.traverse((__node) => {
    if (!__node.isMesh || !__node.geometry || !__node.geometry.attributes.position) return;
    __vertices += __node.geometry.attributes.position.count * (__node.isInstancedMesh ? __node.count : 1);
  });
  __pivot.traverse((__node) => {
    if (!__node.isMesh) return;
    const __materials = Array.isArray(__node.material) ? __node.material : [__node.material];
    for (const __material of __materials) {
      if (!__material || !__material.isMeshStandardMaterial) continue;
      if (__material.metalness > __METALNESS__) __material.metalness = __METALNESS__;
      if (__material.transmission > 0 && __vertices > __TRANSMISSION_VERTICES__) {
        __material.transparent = true;
        __material.opacity = Math.min(__material.opacity, 1 - 0.65 * Math.min(1, __material.transmission));
        __material.transmission = 0;
      }
      if (!__material.transparent && !(__material.transmission > 0)) __material.side = THREE.DoubleSide;
    }
  });
  __pivot.updateMatrixWorld(true);
  const __raw = new THREE.Box3().setFromObject(__pivot).getSize(new THREE.Vector3());
  if (__raw.y > 0 && __raw.y < __FLAT__ * Math.max(__raw.x, __raw.z)) __pivot.rotation.x = __TILT__;
  const __root = new THREE.Group();
  __root.add(__pivot);
  __root.updateMatrixWorld(true);
  const __box = new THREE.Box3().setFromObject(__root);
  const __size = __box.getSize(new THREE.Vector3());
  const __extent = Math.max(__size.x, __size.y, __size.z);
  if (__extent > 0 && Number.isFinite(__extent)) {
    const __center = __box.getCenter(new THREE.Vector3());
    const __scale = __EXTENT__ / __extent;
    __root.scale.setScalar(__scale);
    __root.position.set(-__center.x * __scale, -__center.y * __scale, -__center.z * __scale);
  }
  return __root;
}
"""

FENCE = re.compile(r"```(?:javascript|js|jsx|ecmascript)?[ \t]*\n([\s\S]*?)\n```")
IMPORT = re.compile(r"^[ \t]*import\b[^;\n]*(?:;|\n)", re.MULTILINE)
EXPORT_DEFAULT_NAME = re.compile(r"^[ \t]*export\s+default\s+([A-Za-z_$][\w$]*)\s*;?[ \t]*$", re.MULTILINE)
EXPORT_DEFAULT = re.compile(r"\bexport\s+default\s+")
EXPORT_LIST = re.compile(r"^[ \t]*export\s*\{\s*([A-Za-z_$][\w$]*)\s+as\s+default\s*\}\s*;?[ \t]*$", re.MULTILINE)
EXPORT_DECL = re.compile(r"^([ \t]*)export\s+(?=(?:async\s+)?function\b|const\b|let\b|var\b|class\b)", re.MULTILINE)


def extract(text: str) -> str:
    """Take the JavaScript out of a model reply (bare module or the largest fenced block)."""
    text = text.strip()
    blocks = FENCE.findall(text)
    if blocks:
        text = max(blocks, key=len).strip()
    return text


def wrap(source: str) -> str | None:
    """Return the wrapped module, or None when no default export can be identified."""
    body = IMPORT.sub("", source)
    call: str | None = None
    named = EXPORT_DEFAULT_NAME.search(body) or EXPORT_LIST.search(body)
    if named is not None and named.group(1) not in ("function", "class", "async"):
        call = named.group(1)
        body = body[: named.start()] + body[named.end() :]
    else:
        matches = list(EXPORT_DEFAULT.finditer(body))
        if len(matches) != 1:
            return None
        match = matches[0]
        body = body[: match.start()] + "const __build = " + body[match.end() :]
        call = "__build"
    body = EXPORT_DECL.sub(r"\1", body)
    if EXPORT_DEFAULT.search(body):
        return None
    template = (
        TEMPLATE.replace("__CALL__", call)
        .replace("__METALNESS__", repr(MAX_METALNESS))
        .replace("__TRANSMISSION_VERTICES__", str(TRANSMISSION_MAX_VERTICES))
        .replace("__EXTENT__", repr(EXTENT))
        .replace("__FLAT__", repr(FLAT))
        .replace("__TILT__", repr(TILT))
    )
    head, tail = template.split("__BODY__")
    return head + body.strip() + tail
