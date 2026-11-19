# SPDX-FileCopyrightText: 2025 Blender Foundation
#
# SPDX-License-Identifier: GPL-2.0-or-later

import bpy
from bpy.types import Operator
from bpy.props import BoolProperty, CollectionProperty, StringProperty
from bpy_extras.io_utils import ImportHelper
from bpy_extras.node_utils import connect_sockets
from .. import __package__ as base_package

from os import path
from mathutils import Vector

from ..utils.nodes import (
    NWBase,
    nw_check,
    nw_check_active,
    nw_check_node_type,
    nw_check_space_type,
    force_update,
)
from ..utils.paths import (
    TextureSocket,
    match_files_to_socket_names,
    split_into_components,
)


#### ------------------------------ OPERATORS ------------------------------ ####

class NODE_OT_add_principled_setup(Operator, NWBase, ImportHelper):
    bl_idname = "node.nw_add_textures_for_principled"
    bl_label = "Add Principled Texture Setup"
    bl_description = "Add a texture node setup for Principled BSDF"
    bl_options = {'REGISTER', 'UNDO'}

    directory: StringProperty(
        name='Directory',
        subtype='DIR_PATH',
        default='',
        description='Folder to search in for image files'
    )
    files: CollectionProperty(
        type=bpy.types.OperatorFileListElement,
        options={'HIDDEN', 'SKIP_SAVE'}
    )

    relative_path: BoolProperty(
        name='Relative Path',
        description='Set the file path relative to the blend file, when possible',
        default=True
    )

    order = [
        "filepath",
        "files",
    ]

    def draw(self, context):
        layout = self.layout
        layout.alignment = 'LEFT'

        layout.prop(self, 'relative_path')

    @classmethod
    def poll(cls, context):
        return (nw_check(cls, context)
                and nw_check_active(cls, context)
                and nw_check_space_type(cls, context, {'ShaderNodeTree'})
                and nw_check_node_type(cls, context, 'BSDF_PRINCIPLED'))

    def execute(self, context):
        # Check if everything is ok
        if not self.directory:
            self.report({'INFO'}, 'No folder selected')
            return {'CANCELLED'}
        if not self.files[:]:
            self.report({'INFO'}, 'No files selected')
            return {'CANCELLED'}

        tree = context.space_data.edit_tree
        nodes = tree.nodes
        links = tree.links
        active_node = nodes.active

        # Filter textures names for texture-types in filenames.
        tags = context.preferences.addons[base_package].preferences.principled_tags
        normal_abbr = tags.normal.split(' ')
        bump_abbr = tags.bump.split(' ')
        gloss_abbr = tags.gloss.split(' ')
        rough_abbr = tags.rough.split(' ')
        socketnames = [
            TextureSocket('Displacement', tags.displacement.split(' ')),
            TextureSocket('Base Color', tags.base_color.split(' ')),
            TextureSocket('Metallic', tags.metallic.split(' ')),
            TextureSocket('Specular IOR Level', tags.specular.split(' ')),
            TextureSocket('Roughness', rough_abbr + gloss_abbr),
            TextureSocket('Bump', bump_abbr),
            TextureSocket('Normal', normal_abbr),
            TextureSocket('Transmission Weight', tags.transmission.split(' ')),
            TextureSocket('Emission Color', tags.emission.split(' ')),
            TextureSocket('Alpha', tags.alpha.split(' ')),
            TextureSocket('Ambient Occlusion', tags.ambient_occlusion.split(' ')),
        ]

        match_files_to_socket_names(self.files, socketnames)
        # The sockets with a file found, and the file.
        matched_files = [(s.name, s.file_name) for s in socketnames if s.file_name
                         and path.exists(bpy.path.abspath(self.directory) + s.file_name)]
        if not matched_files:
            self.report({'INFO'}, 'No matching images found')
            print('No matching images found')
            return {'CANCELLED'}

        # Don't override path earlier as os.path is used to check the absolute path
        import_path = self.directory
        if self.relative_path:
            if bpy.data.filepath:
                try:
                    import_path = bpy.path.relpath(self.directory)
                except ValueError:
                    pass

        # Add found images
        print('\nMatched Textures:')
        texture_nodes = []
        disp_texture = None
        ao_texture = None
        normal_node = None
        normal_node_texture = None
        bump_node = None
        bump_node_texture = None
        roughness_node = None
        for i, (socket_name, file_name) in enumerate(matched_files):
            print(i, socket_name, file_name)

            # DISPLACEMENT NODES
            if socket_name == 'Displacement':
                disp_texture = nodes.new(type='ShaderNodeTexImage')
                img = bpy.data.images.load(path.join(import_path, file_name))
                disp_texture.image = img
                disp_texture.label = 'Displacement'
                if disp_texture.image:
                    disp_texture.image.colorspace_settings.is_data = True

                # Add displacement offset nodes
                disp_node = nodes.new(type='ShaderNodeDisplacement')
                # Align the Displacement node under the active Principled BSDF node
                disp_node.location = active_node.location + Vector((100, -700))
                connect_sockets(disp_node.inputs[0], disp_texture.outputs[0])

                # TODO Turn on true displacement in the material
                # Too complicated for now

                # Find output node
                output_node = [n for n in nodes if n.bl_idname == 'ShaderNodeOutputMaterial']
                if output_node:
                    if not output_node[0].inputs[2].is_linked:
                        connect_sockets(output_node[0].inputs[2], disp_node.outputs[0])

                continue

            # BUMP NODES
            elif socket_name == 'Bump':
                # Test if new texture node is bump map
                fname_components = split_into_components(file_name)
                match_bump = set(bump_abbr).intersection(set(fname_components))
                if match_bump:
                    # If Bump add bump node in between
                    bump_node_texture = nodes.new(type='ShaderNodeTexImage')
                    img = bpy.data.images.load(path.join(import_path, file_name))
                    img.colorspace_settings.is_data = True
                    bump_node_texture.image = img
                    bump_node_texture.label = 'Bump'

                    # Add bump node
                    bump_node = nodes.new(type='ShaderNodeBump')
                    connect_sockets(bump_node.inputs[3], bump_node_texture.outputs[0])
                    connect_sockets(active_node.inputs['Normal'], bump_node.outputs[0])
                continue

            # NORMAL NODES
            elif socket_name == 'Normal':
                # Test if new texture node is normal map
                fname_components = split_into_components(file_name)
                match_normal = set(normal_abbr).intersection(set(fname_components))
                if match_normal:
                    # If Normal add normal node in between
                    normal_node_texture = nodes.new(type='ShaderNodeTexImage')
                    img = bpy.data.images.load(path.join(import_path, file_name))
                    img.colorspace_settings.is_data = True
                    normal_node_texture.image = img
                    normal_node_texture.label = 'Normal'

                    # Add normal node
                    normal_node = nodes.new(type='ShaderNodeNormalMap')
                    connect_sockets(normal_node.inputs[1], normal_node_texture.outputs[0])
                    # Connect to bump node if it was created before, otherwise to the BSDF
                    if bump_node is None:
                        connect_sockets(active_node.inputs[socket_name], normal_node.outputs[0])
                    else:
                        connect_sockets(bump_node.inputs[socket_name], normal_node.outputs[socket_name])
                continue

            # AMBIENT OCCLUSION TEXTURE
            elif socket_name == 'Ambient Occlusion':
                ao_texture = nodes.new(type='ShaderNodeTexImage')
                img = bpy.data.images.load(path.join(import_path, file_name))
                ao_texture.image = img
                ao_texture.label = socket_name
                if ao_texture.image:
                    ao_texture.image.colorspace_settings.is_data = True

                continue

            if not active_node.inputs[socket_name].is_linked:
                # No texture node connected -> add texture node with new image
                texture_node = nodes.new(type='ShaderNodeTexImage')
                img = bpy.data.images.load(path.join(import_path, file_name))
                texture_node.image = img

                if socket_name == 'Roughness':
                    # Test if glossy or roughness map
                    fname_components = split_into_components(file_name)
                    match_rough = set(rough_abbr).intersection(set(fname_components))
                    match_gloss = set(gloss_abbr).intersection(set(fname_components))

                    if match_rough:
                        # If Roughness nothing to do.
                        connect_sockets(active_node.inputs[socket_name], texture_node.outputs[0])

                    elif match_gloss:
                        # If Gloss Map add invert node
                        invert_node = nodes.new(type='ShaderNodeInvert')
                        connect_sockets(invert_node.inputs[1], texture_node.outputs[0])

                        connect_sockets(active_node.inputs[socket_name], invert_node.outputs[0])
                        roughness_node = texture_node

                else:
                    # This is a simple connection Texture --> Input slot
                    connect_sockets(active_node.inputs[socket_name], texture_node.outputs[0])

                # Use non-color except for color inputs
                if socket_name not in ['Base Color', 'Emission Color'] and texture_node.image:
                    texture_node.image.colorspace_settings.is_data = True

            else:
                # If already texture connected. add to node list for alignment
                texture_node = active_node.inputs[socket_name].links[0].from_node

            # This are all connected texture nodes
            texture_nodes.append(texture_node)
            texture_node.label = socket_name

        if disp_texture:
            texture_nodes.append(disp_texture)
        if bump_node_texture:
            texture_nodes.append(bump_node_texture)
        if normal_node_texture:
            texture_nodes.append(normal_node_texture)

        if ao_texture:
            # We want the ambient occlusion texture to be the top most texture node
            texture_nodes.insert(0, ao_texture)

        # Alignment
        for i, texture_node in enumerate(texture_nodes):
            offset = Vector((-550, (i * -280) + 200))
            texture_node.location = active_node.location + offset

        if normal_node:
            # Extra alignment if normal node was added
            normal_node.location = normal_node_texture.location + Vector((300, 0))

        if bump_node:
            # Extra alignment if bump node was added
            bump_node.location = bump_node_texture.location + Vector((300, 0))

        if roughness_node:
            # Alignment of invert node if glossy map
            invert_node.location = roughness_node.location + Vector((300, 0))

        # Add texture input + mapping
        mapping = nodes.new(type='ShaderNodeMapping')
        mapping.location = active_node.location + Vector((-1050, 0))
        if len(texture_nodes) > 1:
            # If more than one texture add reroute node in between
            reroute = nodes.new(type='NodeReroute')
            texture_nodes.append(reroute)
            tex_coords = Vector((texture_nodes[0].location.x,
                                 sum(n.location.y for n in texture_nodes) / len(texture_nodes)))
            reroute.location = tex_coords + Vector((-50, -120))
            for texture_node in texture_nodes:
                connect_sockets(texture_node.inputs[0], reroute.outputs[0])
            connect_sockets(reroute.inputs[0], mapping.outputs[0])
        else:
            connect_sockets(texture_nodes[0].inputs[0], mapping.outputs[0])

        # Connect texture_coordinates to mapping node
        texture_input = nodes.new(type='ShaderNodeTexCoord')
        texture_input.location = mapping.location + Vector((-200, 0))
        connect_sockets(mapping.inputs[0], texture_input.outputs[2])

        # Create frame around tex coords and mapping
        frame = nodes.new(type='NodeFrame')
        frame.label = 'Mapping'
        mapping.parent = frame
        texture_input.parent = frame
        frame.update()

        # Create frame around texture nodes
        frame = nodes.new(type='NodeFrame')
        frame.label = 'Textures'
        for tnode in texture_nodes:
            tnode.parent = frame
        frame.update()

        # Just to be sure
        active_node.select = False
        nodes.update()
        links.update()
        force_update(context)
        return {'FINISHED'}
