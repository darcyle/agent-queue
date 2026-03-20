# Plan: Layer Z-Depth Management and Page Tiling System

## Background & Design

### Problem Statement

The level editor needs a proper layer depth management system and page-based tiling architecture. Currently:
1. Layers lack distinct z-depth values, risking visual overlap
2. Pages have no visible boundaries to indicate active/visible state
3. Pages are not reusable — they should function as instanced, tileable objects

### Key Design Principles

- **Layers are depth planes**: Each layer renders at a unique z-depth. No two layers share the same depth value.
- **Pages are instanced templates**: A page is a reusable content container. Multiple instances of the same page share state — editing one updates all instances.
- **Pages tile in 2D**: Pages can be placed in any grid position (horizontal/vertical) within a layer.
- **Cross-layer page sharing**: The same page template can appear in multiple layers, each rendering at that layer's z-depth.

### Architecture Overview

```
Level
├── Layer 0  (z-depth: 0.0, e.g. "Background")
│   ├── PageInstance(page_id="bg-sky", grid_pos=(0,0))
│   ├── PageInstance(page_id="bg-sky", grid_pos=(1,0))  ← same page, tiled
│   └── PageInstance(page_id="bg-hills", grid_pos=(0,1))
├── Layer 1  (z-depth: 1.0, e.g. "Midground")
│   ├── PageInstance(page_id="terrain-a", grid_pos=(0,0))
│   └── PageInstance(page_id="terrain-b", grid_pos=(1,0))
└── Layer 2  (z-depth: 2.0, e.g. "Foreground")
    └── PageInstance(page_id="terrain-a", grid_pos=(0,0))  ← shared with Layer 1
```

### Data Structures

```python
from dataclasses import dataclass, field
from typing import Optional
import uuid


@dataclass
class Page:
    """A reusable content template. All instances of a page share this state."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    width: int = 256   # pixels
    height: int = 256  # pixels
    content: dict = field(default_factory=dict)  # tile data, objects, etc.
    # content is the shared mutable state — any instance editing this
    # page modifies content for ALL instances

    def clone(self) -> "Page":
        """Create an independent copy (breaks instance sharing)."""
        import copy
        return Page(
            id=str(uuid.uuid4()),
            name=f"{self.name} (copy)",
            width=self.width,
            height=self.height,
            content=copy.deepcopy(self.content),
        )


@dataclass
class PageInstance:
    """A placement of a Page within a Layer at a specific grid position."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    page_id: str = ""          # references Page.id
    grid_x: int = 0            # grid column position
    grid_y: int = 0            # grid row position
    # No content here — content lives on the Page object


@dataclass
class Layer:
    """A depth plane containing page instances."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    z_depth: float = 0.0       # unique depth value; higher = closer to camera
    visible: bool = True
    locked: bool = False
    opacity: float = 1.0
    page_instances: list[PageInstance] = field(default_factory=list)

    def add_page(self, page_id: str, grid_x: int, grid_y: int) -> PageInstance:
        """Place a page instance at the given grid position."""
        for inst in self.page_instances:
            if inst.grid_x == grid_x and inst.grid_y == grid_y:
                raise ValueError(
                    f"Grid position ({grid_x}, {grid_y}) already occupied "
                    f"by page instance {inst.id}"
                )
        instance = PageInstance(page_id=page_id, grid_x=grid_x, grid_y=grid_y)
        self.page_instances.append(instance)
        return instance

    def remove_page_at(self, grid_x: int, grid_y: int) -> Optional[PageInstance]:
        """Remove and return the page instance at the given grid position."""
        for i, inst in enumerate(self.page_instances):
            if inst.grid_x == grid_x and inst.grid_y == grid_y:
                return self.page_instances.pop(i)
        return None


@dataclass
class Level:
    """Top-level container for all layers and the page library."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    page_library: dict[str, Page] = field(default_factory=dict)  # page_id -> Page
    layers: list[Layer] = field(default_factory=list)
    cell_width: int = 256
    cell_height: int = 256

    def add_layer(self, name: str, z_depth: Optional[float] = None) -> Layer:
        """Add a new layer. Auto-assigns z_depth if not specified."""
        if z_depth is None:
            z_depth = self._next_z_depth()
        if any(l.z_depth == z_depth for l in self.layers):
            raise ValueError(f"z_depth {z_depth} already in use")
        layer = Layer(name=name, z_depth=z_depth)
        self.layers.append(layer)
        self._sort_layers()
        return layer

    def reorder_layer(self, layer_id: str, new_z_depth: float) -> None:
        """Move a layer to a new z-depth."""
        if any(l.z_depth == new_z_depth and l.id != layer_id for l in self.layers):
            raise ValueError(f"z_depth {new_z_depth} already in use")
        for layer in self.layers:
            if layer.id == layer_id:
                layer.z_depth = new_z_depth
                break
        self._sort_layers()

    def _next_z_depth(self) -> float:
        """Return the next available z-depth (above all existing layers)."""
        if not self.layers:
            return 0.0
        return max(l.z_depth for l in self.layers) + 1.0

    def _sort_layers(self) -> None:
        """Keep layers sorted by z_depth (back to front)."""
        self.layers.sort(key=lambda l: l.z_depth)

    def register_page(self, page: Page) -> None:
        """Add a page to the library for use across layers."""
        self.page_library[page.id] = page

    def get_page(self, page_id: str) -> Optional[Page]:
        """Retrieve a page from the library."""
        return self.page_library.get(page_id)
```

### Z-Depth Management Rules

1. **Uniqueness**: No two layers may share the same `z_depth` value. All mutation methods enforce this.
2. **Ordering**: Layers are always sorted by `z_depth` ascending (lowest = furthest back).
3. **Rendering order**: Iterate layers in sorted order; each layer renders at its `z_depth` plane.
4. **Auto-assignment**: When creating a layer without specifying z_depth, the system assigns `max_existing + 1.0`.
5. **Redistribution**: A utility method can evenly redistribute z-depths (e.g., after many insertions create awkward values like 0.0, 0.5, 0.75, 0.875...).

### Page Instancing Model

Pages use a **flyweight pattern**:
- `Page` objects live in `Level.page_library` and hold the shared mutable content.
- `PageInstance` objects are lightweight references (`page_id` + grid position).
- Editing a page's content through any instance updates the `Page` in the library — all other instances immediately reflect the change.
- To break sharing, use `Page.clone()` which creates a new `Page` with a new ID and deep-copied content.
- The same `page_id` can appear in instances across different layers, rendering the same content at different z-depths.

### Page Border Rendering

Page borders need to clearly communicate:
- **Page boundaries**: Where one page instance ends and another begins
- **Active page**: Which page instance is currently selected/being edited
- **Shared instances**: Visual indicator when multiple instances share the same page

```
Border rendering approach:
- Default border:   1px solid #444 (subtle grid lines)
- Hovered border:   1px solid #888 (highlight on mouseover)
- Selected border:  2px solid #4A9EFF (bright blue, active editing)
- Shared indicator: Small colored dot/badge in corner when page has >1 instance
- Empty cell:       Dashed 1px #333 border (shows available grid slots)
```

### Tiling System

Pages tile on a 2D integer grid within each layer:
- Grid position `(grid_x, grid_y)` maps to pixel position `(grid_x * cell_width, grid_y * cell_height)`.
- Grid extends infinitely in all directions (negative coordinates allowed).
- Each grid cell holds at most one page instance per layer.
- Different layers can have different pages at the same grid position (they render at different z-depths).

Tiling operations:
- **Fill region**: Place the same page across a rectangular range of grid cells.
- **Repeat pattern**: Tile a sequence of pages in a direction (e.g., [A, B, A, B] horizontally).
- **Mirror**: Flip a page's content when placing (requires render-time transform flag on PageInstance).

### Serialization Format

```json
{
  "id": "level-001",
  "name": "Forest Stage",
  "cell_width": 256,
  "cell_height": 256,
  "page_library": {
    "page-abc": {
      "name": "Grass Tile",
      "width": 256,
      "height": 256,
      "content": { "tiles": [], "objects": [] }
    }
  },
  "layers": [
    {
      "id": "layer-001",
      "name": "Background",
      "z_depth": 0.0,
      "visible": true,
      "locked": false,
      "opacity": 1.0,
      "page_instances": [
        { "id": "inst-001", "page_id": "page-abc", "grid_x": 0, "grid_y": 0 },
        { "id": "inst-002", "page_id": "page-abc", "grid_x": 1, "grid_y": 0 }
      ]
    }
  ]
}
```

---

## Phase 1: Core data models and z-depth management

Create the foundational data structures for `Page`, `PageInstance`, `Layer`, and `Level` as defined above. Implement:
- `Layer` with z-depth field and uniqueness enforcement
- `Level.add_layer()` with auto z-depth assignment
- `Level.reorder_layer()` for moving layers between depths
- Z-depth redistribution utility (evenly space all layers)
- Layer sorting (always maintained in z-depth order)
- Unit tests for all z-depth operations (uniqueness, ordering, auto-assignment, redistribution)

## Phase 2: Page library and instancing system

Implement the page instancing (flyweight) pattern:
- `Page` model with shared mutable content and `clone()` method
- `PageInstance` as a lightweight reference (page_id + grid position)
- `Level.page_library` as the central page registry
- `Level.register_page()` and `Level.get_page()` methods
- `Layer.add_page()` with grid position conflict detection
- `Layer.remove_page_at()` for removing instances
- Verify that editing a `Page`'s content is reflected in all instances across all layers
- Unit tests for instancing, shared state, cloning, and cross-layer page sharing

## Phase 3: Page border rendering

Implement the visual page boundary system:
- Render function that draws borders around each page instance's grid cell
- Border styles: default (subtle), hovered (highlight), selected (bright), empty cell (dashed)
- Shared-instance indicator (badge/dot when a page has multiple instances)
- Active page highlight (selected page instance gets distinct border)
- Camera/viewport-aware rendering (only draw borders for visible cells)
- Integration with the existing rendering pipeline at the appropriate z-depth

## Phase 4: Tiling operations and grid tools

Implement tiling utilities for placing pages across grid regions:
- `fill_region(layer, page_id, x_range, y_range)` — fill a rectangular area with a single page
- `tile_pattern(layer, page_ids, direction, start, count)` — repeat a sequence of pages in a direction
- Grid coordinate system: `(grid_x, grid_y)` to pixel position mapping using `cell_width`/`cell_height`
- Support for negative grid coordinates (infinite grid in all directions)
- One-page-per-cell-per-layer constraint enforcement
- UI controls for selecting tiling direction and page assignment
- Unit tests for fill, pattern tiling, and boundary conditions

## Phase 5: Serialization and persistence

Implement save/load for the full level structure:
- JSON serialization matching the format defined in the design section
- `Level.to_dict()` / `Level.from_dict()` round-trip serialization
- Page library serialization (pages saved once, referenced by ID in instances)
- Validation on load: z-depth uniqueness, page_id references exist, no grid position conflicts
- Migration support: version field in the format for future schema changes
- Unit tests for round-trip serialization and validation error handling
