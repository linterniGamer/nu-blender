import bmesh
import bpy
import io
import math
import mathutils
import os
from PIL import Image

from .files.nup import Nup, NuPrimType, RtlSet, RtlType
from .files.nu import (
    NuAlphaMode,
    NuAlphaTest,
    NuAlphaTestMapping,
    NuAnimComponent,
    NuPlatform,
    NuTextureType,
)
from .files.ter import Ter, TerType


def import_nup(context, operator: bpy.types.Operator):
    (path, filename) = os.path.split(operator.filepath)
    (scene_name, ext) = os.path.splitext(filename)

    # Detect scene format from file extension
    match ext.lower():
        case ".nup":
            platform = NuPlatform.PC
        case ".nux":
            platform = NuPlatform.XBOX
        case _:
            platform = None

    # Load scene files, including scene definition, lights, and configuration.
    with open(operator.filepath, "rb") as file:
        data = file.read()
        nup = Nup(data, platform)
        
    # Warn if platform doesn't match.
    if nup.platform != platform:
        operator.report(
            {"WARNING"},
            f"Warning: Detected platform {nup.platform} does not match expected platform {platform} based on file extension.",
        )

    bpy.ops.scene.new()

    scene = bpy.context.scene
    scene.name = scene_name
    scene.render.fps = 60

    obj_layer = scene.view_layers[0]

    terrain_layer = scene.view_layers.new("Terrain")
    terrain_layer.use = False

    image_names = []
    for texture in nup.textures:
        match texture.type:
            case NuTextureType.DXT1:
                decoder = "DXT1"
            case NuTextureType.DXT5:
                decoder = "DXT5"
            case NuTextureType.DDS:
                decoder = None

        if decoder == None:
            image = Image.open(io.BytesIO(texture.data), formats=["DDS"])
        else:
            image = Image.frombytes(
                "RGBA", (texture.width, texture.height), texture.data, decoder
            )
        image_data = image.getdata()

        blend_img = bpy.data.images.new(
            "Texture", texture.width, texture.height, alpha=True
        )
        blend_img.pixels = [item / 255.0 for t in image_data for item in t]
        blend_img.file_format = "PNG"
        blend_img.pack()

        image_names.append(blend_img.name)

    # Get alpha test mapping for platform.
    atst_mapping = NuAlphaTestMapping.PLATFORM_MAPPING[nup.platform or platform]

    material_names = []
    for material in nup.materials:
        blend_mat = bpy.data.materials.new("Material")
        material_names.append(blend_mat.name)

        blend_mat.use_nodes = True
        node_tree = blend_mat.node_tree

        # This doesn't affect rendering, but shows up in layout mode. It can
        # serve as a hint for how it will be rendered, so setting it here.
        blend_mat.diffuse_color = (
            material.diffuse.r,
            material.diffuse.g,
            material.diffuse.b,
            material.alpha,
        )

        # Vertex color node to get vertex colors from the mesh.
        vert_color_node = node_tree.nodes.new("ShaderNodeVertexColor")
        vert_color_node.layer_name = "Col"

        # Function to get the appropriate source node based on alpha mode.
        # This node will have its color input linked to the output of the
        # color mixing node. The returned node's output will be used as the
        # base color for the rest of the shader.
        def get_source_node(color_mix_node):
            match material.alpha_mode():
                case NuAlphaMode.MODE2 | NuAlphaMode.MODE5:
                    # Additive blending. Use emissive material to simulate.
                    # When we combine with transparency later, this will
                    # produce the desired effect.
                    source_node = node_tree.nodes.new("ShaderNodeEmission")
                    source_node.inputs["Strength"].default_value = 1.0

                    node_tree.links.new(
                        color_mix_node.outputs["Color"], source_node.inputs["Color"]
                    )
                    return source_node
                case NuAlphaMode.MODE3:
                    # Subtractive blending, equivalent to blending with black.
                    # Use emissive material to simulate. When we combine with
                    # transparency later, this will produce the desired effect.
                    source_node = node_tree.nodes.new("ShaderNodeEmission")
                    source_node.inputs["Strength"].default_value = 1.0
                    source_node.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
                    return source_node
                case _:
                    # No special handling needed. Standard alpha via lerp.
                    source_node = node_tree.nodes.new("ShaderNodeBsdfDiffuse")
                    source_node.inputs["Roughness"].default_value = 1.0

                    node_tree.links.new(
                        color_mix_node.outputs["Color"], source_node.inputs["Color"]
                    )
                    return source_node

        def get_alpha_output(alpha_pretest_pin):
            # Invert alpha for transparency shader, which uses 0 = opaque,
            # 1 = transparent.
            alpha_invert_node = node_tree.nodes.new("ShaderNodeMath")
            alpha_invert_node.operation = "SUBTRACT"
            alpha_invert_node.inputs[0].default_value = 1.0

            # Use vertex color alpha channel in absence of texture.
            node_tree.links.new(alpha_pretest_pin, alpha_invert_node.inputs[1])

            # Handle alpha testing if needed.
            # We do this by creating a comparison node that outputs 1.0 if the
            # alpha test passes and 0.0 otherwise, and then taking the minimum
            # of this value and the inverted alpha. This way, if the alpha test
            # fails, the output will be 0.0 (fully opaque),
            atst_raw = material.alpha_test()
            match atst_mapping[atst_raw]:
                case NuAlphaTest.GREATER_EQUAL:
                    cmp_node_op = "LESS_THAN"
                case NuAlphaTest.LESS_EQUAL:
                    cmp_node_op = "GREATER_THAN"
                case NuAlphaTest.NONE:
                    cmp_node_op = None

            if cmp_node_op is not None:
                # Create a comparison node to implement alpha testing.
                alpha_cmp_node = node_tree.nodes.new("ShaderNodeMath")
                alpha_cmp_node.operation = cmp_node_op
                alpha_cmp_node.inputs[1].default_value = material.alpha_ref() / 255.0

                node_tree.links.new(alpha_pretest_pin, alpha_cmp_node.inputs[0])

                # Create a minimum node to combine alpha test result and
                # allow alpha blending if applicable.
                alpha_min_node = node_tree.nodes.new("ShaderNodeMath")
                alpha_min_node.operation = "MINIMUM"

                node_tree.links.new(alpha_cmp_node.outputs[0], alpha_min_node.inputs[0])

                node_tree.links.new(
                    alpha_invert_node.outputs[0], alpha_min_node.inputs[1]
                )

                return alpha_min_node.outputs[0]
            else:
                return alpha_invert_node.outputs[0]

        # Build shader node tree. If there's a texture,
        # we use this for diffuse or emissive color.
        if material.texture_idx != None:
            texture_node = node_tree.nodes.new("ShaderNodeTexImage")
            texture_node.image = bpy.data.images[image_names[material.texture_idx]]

            # Multiply texture color and vertex color for the final unlighted
            # color. This is not game-accurate, but a quick approximation.
            color_mix_node = node_tree.nodes.new("ShaderNodeMixRGB")
            color_mix_node.blend_type = "MULTIPLY"

            node_tree.links.new(
                texture_node.outputs["Color"], color_mix_node.inputs["Color1"]
            )

            node_tree.links.new(
                vert_color_node.outputs["Color"], color_mix_node.inputs["Color2"]
            )

            # Grab basic color source from helper function.
            source_node = get_source_node(color_mix_node)

            # Initialize output node.
            output_node = node_tree.nodes.get("Material Output") or node_tree.nodes.new(
                "ShaderNodeOutputMaterial"
            )

            # If the alpha attribute is set, we need to handle transparency.
            # We do so by generating a transparency shader and mixing it with
            # the main shader. The exact method depends on the alpha mode, but
            # generally we either use the texture alpha channel or the
            # brightness of the texture.
            match material.alpha_mode():
                case NuAlphaMode.MODE1 | NuAlphaMode.MODE10:
                    # Multiply texture alpha and vertex color alpha.
                    alpha_mix_node = node_tree.nodes.new("ShaderNodeMath")
                    alpha_mix_node.operation = "MULTIPLY"

                    node_tree.links.new(
                        vert_color_node.outputs["Alpha"], alpha_mix_node.inputs[0]
                    )

                    node_tree.links.new(
                        texture_node.outputs["Alpha"], alpha_mix_node.inputs[1]
                    )

                    alpha_pretest_pin = alpha_mix_node.outputs[0]
                case NuAlphaMode.MODE2 | NuAlphaMode.MODE3 | NuAlphaMode.MODE5:
                    # Interpret pixel brightness of texture as alpha.
                    alpha_pretest_pin = texture_node.outputs["Color"]
                case NuAlphaMode.NONE | _:
                    # No alpha handling.
                    alpha_pretest_pin = None

            # If we have an alpha source, process it for transparency.
            if alpha_pretest_pin is not None:
                alpha_output_pin = get_alpha_output(alpha_pretest_pin)

                # Create transparency shader.
                transparent_bsdf_node = node_tree.nodes.new("ShaderNodeBsdfTransparent")

                # Link inverted alpha to transparency shader color.
                node_tree.links.new(
                    alpha_output_pin, transparent_bsdf_node.inputs["Color"]
                )

                # Combine main shader and transparency shader.
                add_shader_node = node_tree.nodes.new("ShaderNodeAddShader")

                node_tree.links.new(source_node.outputs[0], add_shader_node.inputs[0])

                node_tree.links.new(
                    transparent_bsdf_node.outputs["BSDF"], add_shader_node.inputs[1]
                )

                # Link combined shader to output.
                node_tree.links.new(
                    add_shader_node.outputs["Shader"], output_node.inputs["Surface"]
                )
            else:
                # No transparency; link directly.
                node_tree.links.new(
                    source_node.outputs[0], output_node.inputs["Surface"]
                )

        # Otherwise, we just use vertex color for the diffuse/emissive color.
        # We also mix with the material color to avoid pure white in some specific cases.
        else:
            color_mix_node = node_tree.nodes.new("ShaderNodeMixRGB")
            color_mix_node.blend_type = "MULTIPLY"

            # Mix with material color to avoid pure white.
            color_mix_node.inputs["Color1"].default_value = (
                material.diffuse.r,
                material.diffuse.g,
                material.diffuse.b,
                material.alpha,
            )

            node_tree.links.new(
                vert_color_node.outputs["Color"], color_mix_node.inputs["Color2"]
            )

            # Grab basic color source from helper function.
            # Will be either diffuse or emissive shader.
            source_node = get_source_node(color_mix_node)

            # Initialize output node.
            output_node = node_tree.nodes.get("Material Output") or node_tree.nodes.new(
                "ShaderNodeOutputMaterial"
            )

            # If the alpha attribute is set, we need to handle transparency.
            # We do so by generating a transparency shader and mixing it with
            # the main shader. The exact method depends on the alpha mode, but
            # generally we use the vertex color alpha channel.
            if material.alpha_mode() != NuAlphaMode.NONE:
                alpha_output_pin = get_alpha_output(vert_color_node.outputs["Alpha"])

                # Create transparency shader.
                transparent_bsdf_node = node_tree.nodes.new("ShaderNodeBsdfTransparent")

                node_tree.links.new(
                    alpha_output_pin, transparent_bsdf_node.inputs["Color"]
                )

                # Combine main shader and transparency shader.
                add_shader_node = node_tree.nodes.new("ShaderNodeAddShader")

                node_tree.links.new(source_node.outputs[0], add_shader_node.inputs[0])

                node_tree.links.new(
                    transparent_bsdf_node.outputs["BSDF"], add_shader_node.inputs[1]
                )

                # Link combined shader to output.
                node_tree.links.new(
                    add_shader_node.outputs["Shader"], output_node.inputs["Surface"]
                )
            else:
                # No transparency; link directly.
                node_tree.links.new(
                    source_node.outputs[0], output_node.inputs["Surface"]
                )

    action_names = []
    for anim in nup.scene.anim_data:
        if anim is None:
            action_names.append(None)
            continue

        action = bpy.data.actions.new("Anim Data")
        action_names.append(action.name)

        # Only use slots API for Blender 4.4 and above.
        if bpy.app.version < (4, 4, 0):
            fcurves = action.fcurves
        else:
            object_slot = action.slots.new("OBJECT", "Anim Data")
            layer = action.layers.new("Anim Data")
            strip = layer.strips.new()
            bag = strip.channelbag(object_slot, ensure=True)
            fcurves = bag.fcurves

        anim_length = math.floor(anim.length)
        for frame in range(anim_length):
            chunk_idx = frame // 32
            frame_in_chunk = frame - chunk_idx * 32

            if chunk_idx >= len(anim.chunks):
                continue

            # The game has runtime corrections to the coordinate system used in
            # its animations. In order to correctly replicate the effect on
            # rotations, we reconstruct the final transform for each keyframe
            # and decompose it back into its channels for Blender.
            curveset = anim.chunks[chunk_idx].curvesets[0]

            if curveset.has_rotation:
                x_rot = curveset_key_for_frame(
                    curveset, NuAnimComponent.X_ROTATION, frame_in_chunk
                )
                y_rot = curveset_key_for_frame(
                    curveset, NuAnimComponent.Y_ROTATION, frame_in_chunk
                )
                z_rot = curveset_key_for_frame(
                    curveset, NuAnimComponent.Z_ROTATION, frame_in_chunk
                )

                rotation = mathutils.Euler((x_rot, y_rot, z_rot), "XYZ")
            else:
                rotation = mathutils.Euler((0.0, 0.0, 0.0), "XYZ")

            if curveset.has_scale:
                x_scale = curveset_key_for_frame(
                    curveset, NuAnimComponent.X_SCALE, frame_in_chunk
                )
                y_scale = curveset_key_for_frame(
                    curveset, NuAnimComponent.Y_SCALE, frame_in_chunk
                )
                z_scale = curveset_key_for_frame(
                    curveset, NuAnimComponent.Z_SCALE, frame_in_chunk
                )

                scale = mathutils.Vector((x_scale, y_scale, z_scale))
            else:
                scale = mathutils.Vector((1.0, 1.0, 1.0))

            x = curveset_key_for_frame(
                curveset, NuAnimComponent.X_TRANSLATION, frame_in_chunk
            )
            y = curveset_key_for_frame(
                curveset, NuAnimComponent.Y_TRANSLATION, frame_in_chunk
            )
            z = curveset_key_for_frame(
                curveset, NuAnimComponent.Z_TRANSLATION, frame_in_chunk
            )

            transform = mathutils.Matrix.LocRotScale(
                mathutils.Vector((x, y, z)), rotation, scale
            )

            # Correct the coordinate system of the original data, as during the
            # game's runtime.
            for i in range(4):
                transform[i][2] = -transform[i][2]
            for j in range(4):
                transform[2][j] = -transform[2][j]

            # Swap the y and z axes for Blender's sake.
            transform = (
                mathutils.Matrix(
                    (
                        (1.0, 0.0, 0.0, 0.0),
                        (0.0, 0.0, 1.0, 0.0),
                        (0.0, 1.0, 0.0, 0.0),
                        (0.0, 0.0, 0.0, 1.0),
                    )
                )
                @ transform
            )

            translation, rotation, scale = transform.decompose()

            def ensure_curve_for_property(prop, index):
                return fcurves.find(prop, index=index) or fcurves.new(prop, index=index)

            def last_key_value(curve):
                return curve.keyframe_points[-1].co[1]

            def add_keyframe_for_curve(prop, index, value):
                curve = ensure_curve_for_property(prop, index)
                if len(curve.keyframe_points) == 0 or last_key_value(curve) != value:
                    curve.keyframe_points.insert(frame + 1, value)

            # Build the Blender keyframe.
            if curveset.has_rotation:
                w_curve = ensure_curve_for_property("rotation_quaternion", index=0)
                if len(w_curve.keyframe_points) != 0:
                    # Ensure that the quaternion's direction of rotation is
                    # correct for proper interpolation.
                    x_curve = ensure_curve_for_property("rotation_quaternion", index=1)
                    y_curve = ensure_curve_for_property("rotation_quaternion", index=2)
                    z_curve = ensure_curve_for_property("rotation_quaternion", index=3)

                    prev_quat = mathutils.Quaternion(
                        (
                            last_key_value(w_curve),
                            last_key_value(x_curve),
                            last_key_value(y_curve),
                            last_key_value(z_curve),
                        )
                    )

                    if rotation.dot(prev_quat) < 0:
                        rotation = -rotation

                add_keyframe_for_curve("rotation_quaternion", 0, rotation.w)
                add_keyframe_for_curve("rotation_quaternion", 1, rotation.x)
                add_keyframe_for_curve("rotation_quaternion", 2, rotation.y)
                add_keyframe_for_curve("rotation_quaternion", 3, rotation.z)

            if curveset.has_scale:
                add_keyframe_for_curve("scale", 0, scale.x)
                add_keyframe_for_curve("scale", 1, scale.y)
                add_keyframe_for_curve("scale", 2, scale.z)

            add_keyframe_for_curve("delta_location", 0, translation.x)
            add_keyframe_for_curve("delta_location", 1, translation.y)
            add_keyframe_for_curve("delta_location", 2, translation.z)

    # Group instances by their objid so that we can create a single mesh object
    # and provide it to each instance.
    instances_by_obj = {}
    for instance in nup.scene.instances:
        if instances_by_obj.get(instance.obj_idx) is None:
            instances_by_obj[instance.obj_idx] = [instance]
        else:
            instances_by_obj[instance.obj_idx].append(instance)

    # Transform NUP gobjs to Blender meshes.
    for obj_idx, obj in enumerate(nup.scene.objects):
        blend_mesh = bmesh.new()
        geom = obj.geom

        nu_mtl_idx_to_blend = {}
        mesh_materials = []

        # Iterate through geometry, adding vertices for each geom and breaking
        # each primitive into triangles, for which we add faces.
        while geom is not None:
            base_index = len(blend_mesh.verts)

            for vertex in geom.vertices:
                blend_vert = blend_mesh.verts.new(
                    (vertex.position.x, vertex.position.y, vertex.position.z)
                )
                blend_vert.normal = mathutils.Vector(
                    (vertex.normal.x, vertex.normal.y, vertex.normal.z)
                )

            blend_mesh.verts.ensure_lookup_table()

            blend_mat_idx = nu_mtl_idx_to_blend.get(geom.material_idx)
            if blend_mat_idx is None:
                blend_mat_idx = len(mesh_materials)
                nu_mtl_idx_to_blend[geom.material_idx] = blend_mat_idx

                mesh_materials.append(
                    bpy.data.materials[material_names[geom.material_idx]]
                )

            prim = geom.prim

            while prim is not None:
                if prim.type == NuPrimType.NDXTRISTRIP:
                    # Convert triangle strip to triangles and add faces.
                    should_reverse = True
                    for i in range(len(prim.index_buf) - 2):
                        # Preserve winding order. No idea if Blender cares about
                        # it.
                        if should_reverse:
                            corners = [
                                prim.index_buf[i],
                                prim.index_buf[i + 2],
                                prim.index_buf[i + 1],
                            ]
                        else:
                            corners = [
                                prim.index_buf[i],
                                prim.index_buf[i + 1],
                                prim.index_buf[i + 2],
                            ]

                        should_reverse = not should_reverse

                        corners_global = [corner + base_index for corner in corners]

                        # Skip degenerate triangles. We deliberately do this
                        # after flipping the winding.
                        if (
                            corners[0] == corners[1]
                            or corners[0] == corners[2]
                            or corners[1] == corners[2]
                        ):
                            continue

                        if (
                            blend_mesh.faces.get(
                                (
                                    blend_mesh.verts[corners_global[0]],
                                    blend_mesh.verts[corners_global[1]],
                                    blend_mesh.verts[corners_global[2]],
                                )
                            )
                            is not None
                        ):
                            continue

                        # Add a new Blender face for this triangle.
                        face = blend_mesh.faces.new(
                            (
                                blend_mesh.verts[corners_global[0]],
                                blend_mesh.verts[corners_global[1]],
                                blend_mesh.verts[corners_global[2]],
                            )
                        )

                        face.material_index = blend_mat_idx

                        uv_layer = blend_mesh.loops.layers.uv.verify()
                        color_layer = blend_mesh.loops.layers.color.verify()

                        for i, loop in enumerate(face.loops):
                            vert = corners[i]

                            vertex = geom.vertices[vert]

                            loop[uv_layer].uv[0] = vertex.uv[0]
                            loop[uv_layer].uv[1] = vertex.uv[1]

                            vert_color = vertex.colour
                            loop[color_layer] = (
                                vert_color.r,
                                vert_color.g,
                                vert_color.b,
                                vert_color.a,
                            )
                else:
                    return {"CANCELLED"}

                prim = prim.next

            geom = geom.next

        mesh = bpy.data.meshes.new("Object")

        for material in mesh_materials:
            mesh.materials.append(material)

        blend_mesh.to_mesh(mesh)
        blend_mesh.free()

        # Create an object for each instance of this gobj.
        for instance in instances_by_obj.get(obj_idx, []):
            obj = bpy.data.objects.new("Instance", mesh)

            if instance.anim is not None:
                transform = mathutils.Matrix(instance.anim.mtx.rows)
            else:
                transform = mathutils.Matrix(instance.transform.rows)

            transform.transpose()

            transform = (
                mathutils.Matrix(
                    (
                        (1.0, 0.0, 0.0, 0.0),
                        (0.0, 0.0, 1.0, 0.0),
                        (0.0, 1.0, 0.0, 0.0),
                        (0.0, 0.0, 0.0, 1.0),
                    )
                )
                @ transform
            )

            # Animations use the `rotation_quaternion` property, so we need to
            # set the object's rotation mode to match.
            obj.rotation_mode = "QUATERNION"

            obj.matrix_world = transform

            if instance.anim is not None and (
                # Sometimes, anim_idx is out of range because of stale
                # data, so we implicitly dereference by ignoring those cases.
                len(action_names)
                > instance.anim.anim_idx
            ):
                action_name = action_names[instance.anim.anim_idx]

                if action_name is not None:
                    anim_data = obj.animation_data_create()
                    action = bpy.data.actions[action_name]
                    anim_data.action = action

                    # Set the action slot for Blender 4.4 and above.
                    if bpy.app.version >= (4, 4, 0):
                        anim_data.action_slot = action.slots[0]

            bpy.context.collection.objects.link(obj)
            obj.hide_set(True, view_layer=terrain_layer)

            if not instance.is_visible:
                obj.hide_set(True, view_layer=obj_layer)

    for spline in nup.scene.splines:
        curve = bpy.data.curves.new(spline.name, "CURVE")
        blend_spline = curve.splines.new("POLY")
        blend_spline.points.add(len(spline.points) - 1)

        for i, point in enumerate(spline.points):
            blend_spline.points[i].co = (point.x, point.z, point.y, 0.0)

        obj = bpy.data.objects.new(spline.name, curve)
        obj.color = (1.0, 0.0, 0.0, 0.0)
        bpy.context.collection.objects.link(obj)

    # Set the world background color to approximate ambient lighting.
    world = bpy.data.worlds.new("World")
    world.use_nodes = True

    ambient_node = world.node_tree.nodes.new("ShaderNodeAttribute")
    ambient_node.attribute_name = "Ambient"
    ambient_node.attribute_type = "VIEW_LAYER"

    color_bg_node = world.node_tree.nodes.get(
        "Background"
    ) or world.node_tree.nodes.new("ShaderNodeBackground")

    world.node_tree.links.new(
        ambient_node.outputs["Color"], color_bg_node.inputs["Color"]
    )

    # We can configure the world shader to _display_ the world background as
    # black, as it is in-game, even though the lighting contribution comes from
    # the ambient color.
    display_bg_node = world.node_tree.nodes.new("ShaderNodeBackground")
    display_bg_node.inputs["Color"].default_value = (0.0, 0.0, 0.0, 0.0)

    light_node = world.node_tree.nodes.new("ShaderNodeLightPath")

    bg_selector_node = world.node_tree.nodes.new("ShaderNodeMixShader")

    world.node_tree.links.new(
        light_node.outputs["Is Camera Ray"], bg_selector_node.inputs[0]
    )

    world.node_tree.links.new(
        color_bg_node.outputs["Background"], bg_selector_node.inputs[1]
    )

    world.node_tree.links.new(
        display_bg_node.outputs["Background"], bg_selector_node.inputs[2]
    )

    output_node = world.node_tree.nodes.get(
        "World Output"
    ) or world.node_tree.nodes.new("ShaderNodeOutputWorld")

    world.node_tree.links.new(
        bg_selector_node.outputs["Shader"], output_node.inputs["Surface"]
    )

    scene.world = world

    file = open_i(path, scene_name + ".rtl", "rb")
    if file is None:
        return {"FINISHED"}

    data = file.read()
    file.close()

    rtl = RtlSet(data)

    for light in rtl.lights:
        # TODO: Figure out what the heck to do about lights other than point,
        # directional, and ambient.

        blend_light = None
        if light.type == RtlType.AMBIENT:
            # There's no real equivalent, so we set this as a property of the
            # scene.
            scene["Ambient"] = [light.colour.r, light.colour.g, light.colour.b]
        elif light.type == RtlType.POINT:
            blend_light = bpy.data.lights.new("Light", "POINT")
        elif light.type == RtlType.DIRECTIONAL:
            blend_light = bpy.data.lights.new("Light", "SUN")

        if blend_light is not None:
            obj = bpy.data.objects.new("Light", blend_light)

            if light.type == RtlType.POINT:
                obj.location = (light.pos.x, light.pos.z, light.pos.y)
            if light.type == RtlType.DIRECTIONAL:
                blend_light.use_shadow = False

                direction = light.dir
                obj.matrix_world = mathutils.Matrix(
                    (
                        (0.0, direction.x, 0.0, 0.0),
                        (0.0, direction.z, 0.0, 0.0),
                        (0.0, direction.y, 0.0, 0.0),
                        (0.0, 0.0, 0.0, 1.0),
                    )
                )

            blend_light.color = (light.colour.r, light.colour.g, light.colour.b)

            bpy.context.collection.objects.link(obj)

    file = open_i(path, scene_name + ".ter", "rb")
    if file is None:
        return {"FINISHED"}

    data = file.read()
    file.close()

    ter = Ter(data)

    for situ in ter.situs:
        if situ.type == TerType.NORMAL or situ.type == TerType.PLATFORM:
            if situ.type == TerType.NORMAL:
                name = "Normal"
            else:
                name = "Platform"

            for group in situ.groups:
                for ter in group.ters:
                    blend_mesh = bmesh.new()

                    for point in ter.points:
                        blend_mesh.verts.new((point.x, point.z, point.y))

                    blend_mesh.verts.ensure_lookup_table()

                    # This is an awkward way of creating the face, but order is
                    # meaningful when the face is a quad.
                    if len(ter.points) == 4:
                        face = blend_mesh.faces.new(
                            (
                                blend_mesh.verts[0],
                                blend_mesh.verts[1],
                                blend_mesh.verts[3],
                                blend_mesh.verts[2],
                            )
                        )
                    else:
                        face = blend_mesh.faces.new(
                            (
                                blend_mesh.verts[0],
                                blend_mesh.verts[1],
                                blend_mesh.verts[2],
                            )
                        )

                    mesh = bpy.data.meshes.new(name)

                    blend_mesh.to_mesh(mesh)
                    blend_mesh.free()

                    obj = bpy.data.objects.new(name, mesh)
                    obj.location = mathutils.Vector(
                        (situ.location.x, situ.location.z, situ.location.y)
                    )

                    bpy.context.collection.objects.link(obj)
                    obj.hide_set(True, view_layer=obj_layer)
                    obj.hide_render = True
        elif situ.type == TerType.WALL_SPLINE:
            curve = bpy.data.curves.new("Wall Spline", "CURVE")
            blend_spline = curve.splines.new("POLY")
            blend_spline.points.add(len(situ.spline.points) - 1)

            for i, point in enumerate(situ.spline.points):
                blend_spline.points[i].co = (point.x, point.z, 0.0, 0.0)

            obj = bpy.data.objects.new("Wall Spline", curve)

            bpy.context.collection.objects.link(obj)
            obj.hide_set(True, view_layer=obj_layer)
            obj.hide_render = True

    return {"FINISHED"}


def open_i(path, filename, mode):
    filename = filename.lower()
    real_path = None

    for entry in os.listdir(path):
        if entry.lower() == filename:
            real_path = os.path.join(path, entry)
            break

    if real_path == None:
        return None

    return open(real_path, "rb")


def curveset_key_for_frame(curveset, component, frame):
    curve = curveset.curves.get(component)
    if curve is not None:
        key_idx = curve_key_idx_for_frame(curve, frame)
        return curve.keys[key_idx].d
    else:
        return curveset.constants[component]


def curve_key_idx_for_frame(curve, frame):
    frame_mask = 1 << frame + 1
    return (curve.mask & (frame_mask - 1)).bit_count() - 1
