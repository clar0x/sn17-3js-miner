"""Last-resort module from the photo's measured silhouette, used only when every candidate fails.

A lathe follows the silhouette width band by band and takes each band's measured color,
so the prompt still gets a valid, roughly matching object instead of an automatic loss.
"""

from __future__ import annotations

from .analysis import Facts

TEMPLATE = """export default function generate(THREE) {
  const widths = [__WIDTHS__];
  const colors = [__COLORS__];
  const depth = __DEPTH__;
  const count = widths.length;
  const points = [new THREE.Vector2(0, -0.5)];
  for (let i = count - 1; i >= 0; i--) {
    points.push(new THREE.Vector2(Math.max(0.01, widths[i] / 2000), 0.5 - (i + 0.5) / count));
  }
  points.push(new THREE.Vector2(0, 0.5));
  const geometry = new THREE.LatheGeometry(points, 96);
  const position = geometry.attributes.position;
  const rgb = new Float32Array(position.count * 3);
  const color = new THREE.Color();
  for (let k = 0; k < position.count; k++) {
    const band = Math.min(count - 1, Math.max(0, Math.floor((0.5 - position.getY(k)) * count)));
    color.setHex(colors[band]);
    rgb[3 * k] = color.r;
    rgb[3 * k + 1] = color.g;
    rgb[3 * k + 2] = color.b;
  }
  geometry.setAttribute('color', new THREE.BufferAttribute(rgb, 3));
  geometry.computeVertexNormals();
  const material = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.6, metalness: 0.05 });
  const mesh = new THREE.Mesh(geometry, material);
  mesh.scale.set(1, 1, depth);
  const group = new THREE.Group();
  group.add(mesh);
  return group;
}
"""

DEFAULT_WIDTHS = (300, 600, 750, 800, 800, 800, 780, 750, 720, 700, 700, 700, 700, 680, 640, 500)


def module(facts: Facts | None) -> str:
    if facts is None:
        widths = DEFAULT_WIDTHS
        colors = ("#9a9a9a",) * len(widths)
    else:
        widths = facts.widths
        colors = facts.band_colors
    # Wide flat silhouettes are usually not that deep; keep the lathe from becoming a disc.
    widest = max(widths)
    depth = "1" if widest <= 1000 else repr(round(1000 / widest, 3))
    return (
        TEMPLATE.replace("__WIDTHS__", ", ".join(str(w) for w in widths))
        .replace("__COLORS__", ", ".join("0x" + c[1:] for c in colors))
        .replace("__DEPTH__", depth)
    )
