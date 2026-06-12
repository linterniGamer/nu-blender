from enum import Enum

from .types import *

from .nu import *
from .read import *


class NuPlatformException(Exception):
    pass


class Nup:
    HEADER_SIZE = 0x40

    def __init__(self, data, platform=None):
        header = NupHeader(data)
        body = data[0x40:]

        # Load textures.
        texture_data_offset = read_u32(body, header.texture_hdr_offset)
        texture_data_size = read_u32(body, header.texture_hdr_offset + 0x04)
        textures_count = read_i32(body, header.texture_hdr_offset + 0x08)

        # Determine if all textures are DDS to infer platform.
        is_pc = True
        is_xbox = True
        self.textures = []
        for i in range(textures_count):
            texture_header = NuTextureHeader(
                body, header.texture_hdr_offset + 0x0C + i * NuTextureHeader.SIZE
            )

            # Texture size is not stored in the file, so we need to calculate a
            # rough size from the offset of each texture. This works because
            # textures are stored contiguously.
            if i < textures_count - 1:
                next_texture_header = NuTextureHeader(
                    body,
                    header.texture_hdr_offset + 0x0C + (i + 1) * NuTextureHeader.SIZE,
                )

                size_estimate = (
                    next_texture_header.data_offset - texture_header.data_offset
                )
            else:
                size_estimate = texture_data_size - texture_header.data_offset

            offset_in_body = (
                header.texture_hdr_offset
                + 0x0C
                + texture_data_offset
                + texture_header.data_offset
            )

            # Check if texture is DDS. Used to determine platform.
            is_pc = is_pc and texture_header.type == NuTextureType.DDS
            is_xbox = is_xbox and texture_header.type != NuTextureType.DDS

            self.textures.append(
                Texture(body, offset_in_body, size_estimate, texture_header)
            )

        # Determine platform from texture hints.
        if is_pc and is_xbox:
            self.platform = None  # No textures present.
        elif is_pc:
            self.platform = NuPlatform.PC  # All textures are DDS.
        elif is_xbox:
            self.platform = NuPlatform.XBOX  # No DDS textures.
        else:  # Mixed textures.
            raise NuPlatformException(
                "Mixed texture types found; cannot determine platform."
            )

        # Prefer the platform requested by the importer/file extension.
        # Some Xbox .nux files contain texture headers that can be mis-detected
        # as PC, but their scene instance table is still Xbox-sized (0x54 bytes).
        scene_platform = platform or self.platform

        # Load materials.
        materials_count = read_i32(body, header.materials_offset)

        self.materials = []
        for i in range(materials_count):
            material_offset = read_u32(body, header.materials_offset + 0x04 + i * 0x04)

            self.materials.append(NuMaterial(body, material_offset, scene_platform))

        # Load vertex data.
        vertex_bufs_count = read_i32(body, header.vertex_data_offset)

        vertex_bufs = [
            read_vertices(i, header.vertex_data_offset, body)
            for i in range(vertex_bufs_count)
        ]

        self.scene = NuScene(body, header.scene_offset, header, vertex_bufs, scene_platform)


class NupHeader:
    def __init__(self, data):
        self.texture_hdr_offset = read_u32(data, 0x08)
        self.materials_offset = read_u32(data, 0x0C)

        self.vertex_data_offset = read_u32(data, 0x14)
        self.scene_offset = read_u32(data, 0x18)
        self.instances_offset = read_u32(data, 0x1C)


class RtlSet:
    def __init__(self, data):
        version = read_u32(data, 0x00)

        if version < 3:
            raise Exception("RTL version {} unsupported".format(version))
        elif version == 3:
            rtl_count = 64
        else:
            rtl_count = 128

        self.lights = []
        for i in range(rtl_count):
            self.lights.append(Rtl(data, 0x04 + i * Rtl.SIZE))


class Rtl:
    SIZE = 0x8C

    def __init__(self, data, offset):
        self.type = RtlType(read_u8(data, offset + 0x58))

        if self.type == RtlType.POINT:
            self.pos = NuVec(data, offset + 0x00)
        elif self.type == RtlType.DIRECTIONAL:
            self.dir = NuVec(data, offset + 0x0C)

        self.colour = NuColour3(data, offset + 0x18)


class RtlType(Enum):
    INVALID = 0
    AMBIENT = 1
    POINT = 2
    POINTFLICKER = 3
    DIRECTIONAL = 4
    CAMDIR = 5
    POINTBLEND = 6
    ANTILIGHT = 7
    JONFLICKER = 8


class NuScene:
    def __init__(self, data, offset, header, vertex_bufs, platform=None):
        objects_count = read_i32(data, offset + 0x10)
        objects_offset = read_u32(data, offset + 0x14)

        self.objects = []
        for i in range(objects_count):
            objects_offset_i = objects_offset + i * 4
            object_offset = read_u32(data, objects_offset_i)

            self.objects.append(NuObject(data, object_offset, vertex_bufs))

        instances_count = read_i32(data, offset + 0x18)

        # Xbox .nux is not uniform: some files use 0x50-byte instances and
        # others use 0x54-byte instances. The original importer worked for
        # 0x50, so prefer that layout first. If an impossible animation
        # pointer / bad read appears, retry the whole instance table as 0x54.
        #
        # Note: in the 0x50 variant, the next 4 bytes can be level creation
        # metadata/size info and must be ignored. Treating it as part of the
        # instance is what desynchronizes some files.
        def _read_instances_with_stride(instance_size):
            instances = []
            end = header.instances_offset + instances_count * instance_size
            if instances_count < 0 or end > len(data):
                raise ValueError(f"Invalid instance table for stride 0x{instance_size:X}")

            for j in range(instances_count):
                inst_off = header.instances_offset + j * instance_size
                inst = NuInstance(data, inst_off, strict=True)

                # If the layout is wrong, object index / anim pointer often
                # becomes matrix float data such as 0x3F800000. Force fallback.
                if not (-1 <= inst.obj_idx < objects_count):
                    raise ValueError(f"Invalid obj_idx {inst.obj_idx} for stride 0x{instance_size:X}")

                instances.append(inst)
            return instances

        try:
            self.instances = _read_instances_with_stride(NuInstance.SIZE)
            self.instance_size = NuInstance.SIZE
        except ValueError:
            self.instances = _read_instances_with_stride(NuInstance.SIZE_ALTERNATE)
            self.instance_size = NuInstance.SIZE_ALTERNATE

        splines_count = read_i32(data, offset + 0x28)
        splines_offset = read_u32(data, offset + 0x2C)

        self.splines = []
        for i in range(splines_count):
            splines_offset_i = splines_offset + i * NuSpline.SIZE

            self.splines.append(NuSpline(data, splines_offset_i))

        anim_data_offset = read_u32(data, offset + 0x48)
        anim_data_count = read_i32(data, offset + 0x4C)

        self.anim_data = []
        for i in range(anim_data_count):
            anim_data_offset_i = read_u32(data, anim_data_offset + i * 0x04)

            if anim_data_offset_i != 0:
                self.anim_data.append(NuAnimData(data, anim_data_offset_i, header))
            else:
                self.anim_data.append(None)


class NuObject:
    SIZE = 0x70

    geom = None

    def __init__(self, data, offset, vertex_bufs):
        geom_offset = read_u32(data, offset + 0x0C)
        if geom_offset != 0:
            self.geom = NuGeom(data, geom_offset, vertex_bufs)


class NuInstance:
    SIZE = 0x50
    SIZE_ALTERNATE = 0x54

    anim = None

    def __init__(self, data, offset, strict=False):
        self.anim = None
        self.transform = NuMtx(data, offset)
        self.obj_idx = read_i16(data, offset + 0x40)

        flags = read_u32(data, offset + 0x44)

        self.is_visible = (flags & 1) != 0

        anim_offset = read_u32(data, offset + 0x48)
        if anim_offset != 0:
            if anim_offset < len(data) - NuInstAnim.SIZE:
                self.anim = NuInstAnim(data, anim_offset)
            elif strict:
                raise ValueError(f"Invalid instance animation offset 0x{anim_offset:X}")


class NuInstAnim:
    SIZE = 0x60

    def __init__(self, data, offset):
        self.mtx = NuMtx(data, offset)
        self.time_factor = read_f32(data, offset + 0x40)
        self.time_first = read_f32(data, offset + 0x44)
        self.time_interval = read_f32(data, offset + 0x48)
        self.anim_idx = read_u8(data, offset + 0x5C)


class NuSpline:
    SIZE = 0x0C

    def __init__(self, data, offset):
        points_count = read_i16(data, offset)

        name_offset = read_u32(data, offset + 0x04)
        self.name = read_string(data, name_offset)

        points_offset = read_u32(data, offset + 0x08)

        self.points = []
        for i in range(points_count):
            points_offset_i = points_offset + i * NuVec.SIZE

            self.points.append(NuVec(data, points_offset_i))


def read_vertices(i, vertex_data_offset, body):
    vertex_hdr_offset = vertex_data_offset + 0x10 + i * 0x0C

    size = read_u32(body, vertex_hdr_offset)
    buf_offset = read_u32(body, vertex_hdr_offset + 0x08)

    buf_offset = vertex_data_offset + buf_offset

    return body[buf_offset : buf_offset + size]
