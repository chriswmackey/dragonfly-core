# coding: utf-8
"""Dragonfly Building."""
from __future__ import division

import math
try:
    from itertools import izip as zip  # python 2
except ImportError:
    xrange = range  # python 3

from ladybug_geometry.geometry2d import Vector2D, Point2D, LineSegment2D, Polygon2D
from ladybug_geometry.geometry3d import Vector3D, Point3D, Ray3D
from ladybug_geometry_polyskel.polysplit import perimeter_core_subfaces

from honeybee.model import Model
from honeybee.room import Room
from honeybee.shade import Shade
from honeybee.facetype import Wall, Floor, RoofCeiling
from honeybee.boundarycondition import Outdoors, Surface
from honeybee.boundarycondition import boundary_conditions as bcs
from honeybee.typing import clean_string, invalid_dict_error
from honeybee.units import parse_distance_string

from ._base import _BaseGeometry
from .properties import BuildingProperties
from .story import Story
from .roof import RoofSpecification
from .room2d import Room2D
from .windowparameter import _AsymmetricBase
from .skylightparameter import DetailedSkylights
import dragonfly.writer.building as writer


class Building(_BaseGeometry):
    """A complete Building defined by Stories (and optional extra 3D rooms).

    Buildings must have at least one dragonfly Story or one Honeybee Room under
    the room_3ds property.

    Args:
        identifier: Text string for a unique Building ID. Must be < 100 characters
            and not contain any spaces or special characters.
        unique_stories: An array of unique Dragonfly Story objects that
            together form the entire building. Stories input here can be in any
            order but they will be automatically sorted from lowest floor to
            highest floor when they are assigned to the Building. Note that,
            if a given Story is repeated several times over the height of the
            Building, the unique Story included in this list should be the
            first (lowest) Story of the repeated floors. (Default: None).
        room_3ds: An optional array of 3D Honeybee Room objects for additional
            Rooms that are a part of the Building but are not represented within
            the unique_stories. This is useful when there are parts of the Building
            geometry that cannot easily be represented with the extruded floor
            plate and sloped roof assumptions that underlie Dragonfly Room2Ds
            and RoofSpecification. Cases where this input is most useful include
            sloped walls and certain types of domed roofs that become tedious to
            implement with RoofSpecification. Matching the Honeybee Room.story
            property to the Dragonfly Story.display_name of an object within the
            unique_stories will effectively place the Honeybee Room on that Story
            for the purposes of floor_area, exterior_wall_area, etc. However, note
            that the Honeybee Room.multiplier property takes precedence over
            whatever multiplier is assigned to the Dragonfly Story that the
            Room.story may reference. (Default: None).
        sort_stories: A boolean to note whether the unique_stories should be sorted
            from lowest to highest story upon initialization (True) or whether
            the input order of unique_stories should be left as-is. (Default: True).

    Properties:
        * identifier
        * display_name
        * full_id
        * unique_stories
        * unique_room_2ds
        * room_3ds
        * room_3d_faces
        * room_3d_apertures
        * room_3d_doors
        * room_3d_shades
        * has_room_2ds
        * has_room_3ds
        * has_room_2d_plenums
        * room_2d_story_names
        * room_3d_story_names
        * has_zones
        * zone_dict
        * story_count
        * story_count_above_ground
        * unique_stories_above_ground
        * height
        * height_above_ground
        * height_from_first_floor
        * footprint_area
        * floor_area
        * exterior_wall_area
        * exterior_aperture_area
        * volume
        * min
        * max
        * user_data
    """
    __slots__ = ('_unique_stories', '_room_3ds', '_roofs')

    def __init__(self, identifier, unique_stories=None, room_3ds=None,
                 sort_stories=True):
        """A complete Building defined by Stories."""
        # initialize and perform a basic check that there's some geometry
        _BaseGeometry.__init__(self, identifier)  # process the identifier
        if (unique_stories is None or len(unique_stories) == 0) and \
                (room_3ds is None or len(room_3ds) == 0):
            raise ValueError('Building must have some geometry - at least one Story '
                             'or one Room under room_3ds.')

        # process the story geometry
        if unique_stories is not None:
            for story in unique_stories:
                assert isinstance(story, Story), \
                    'Expected dragonfly Story. Got {}'.format(type(story))
                story._parent = self
            if sort_stories:
                unique_stories = \
                    tuple(sorted(unique_stories, key=lambda x: x.floor_height))
            else:
                unique_stories = tuple(unique_stories)
            self._unique_stories = unique_stories
        else:
            self._unique_stories = ()

        # process the room_3d geometry
        if room_3ds is not None:
            for room in room_3ds:
                assert isinstance(room, Room), \
                    'Expected honeybee Room. Got {}'.format(type(room))
                room._parent = self
            # assign stories to any Rooms that lack them
            if not all([r.story is not None for r in room_3ds]):
                Room.stories_by_floor_height(room_3ds)
            self._room_3ds = tuple(room_3ds)
        else:
            self._room_3ds = ()

        # initialize properties
        self._roofs = None  # used under the hood to correctly assign roofs
        self._properties = BuildingProperties(self)  # properties for extensions

    @classmethod
    def from_footprint(cls, identifier, footprint, floor_to_floor_heights,
                       perimeter_offset=0, tolerance=0):
        """Initialize a Building from an array of Face3Ds representing a footprint.

        All of the resulting Room2Ds will have a floor-to-ceiling height equal to the
        Story floor-to-floor height. Also, none of the Room2Ds will have contact
        with the ground or top exposure but the separate_top_bottom_floors method
        can be used to automatically break these floors out from the multiplier
        representation and assign these properties.

        Args:
            identifier: Text string for a unique Building ID. Must be < 100 characters
                and not contain any spaces or special characters.
            footprint: An array of horizontal ladybug-geometry Face3Ds that together
                represent the the footprint of the Building.
            floor_to_floor_heights: An array of float values with a length equal
                to the number of stories in the Building. Each value in the list
                represents the floor_to_floor height of the Story starting from
                the first floor and then moving to the top floor. Note that numbers
                should be in the units system of the footprint geometry.
            perimeter_offset: An optional positive number that will be used to
                offset the perimeter of the footprint to create core/perimeter
                zones. If this value is 0, no offset will occur and each story
                will be represented with a single Room2D per polygon (Default: 0).
            tolerance: The maximum difference between z values at which point vertices
                are considered to be in the same horizontal plane. This is used to check
                that all vertices of the input floor_geometry lie in the same horizontal
                floor plane. Default is 0, which will not perform any check.
        """
        # generate the unique Room2Ds from the footprint
        room_2ds = cls._generate_room_2ds(
            footprint, floor_to_floor_heights[0], perimeter_offset,
            identifier, 1, tolerance)

        # generate the unique stories from the floor_to_floor_heights
        stories = []
        total_height = 0
        prev_flr_to_flr = None
        for i, flr_hgt in enumerate(floor_to_floor_heights):
            if flr_hgt != prev_flr_to_flr:
                if i != 0:
                    rooms = [room.duplicate() for room in room_2ds]
                    move_vec = Vector3D(0, 0, total_height)
                    for j, room in enumerate(rooms):
                        room.move(move_vec)
                        room.floor_to_ceiling_height = flr_hgt
                        room.identifier = \
                            '{}_Floor{}_Room{}'.format(identifier, i + 1, j + 1)
                    if perimeter_offset != 0:  # reset all boundary conditions
                        for room in rooms:
                            room.boundary_conditions = [bcs.outdoors] * len(room)
                        Room2D.solve_adjacency(rooms, tolerance)
                else:
                    rooms = room_2ds
                stories.append(Story(
                    '{}_Floor{}'.format(identifier, i + 1), rooms, flr_hgt))
            else:
                stories[-1].multiplier += 1
            total_height += flr_hgt
            prev_flr_to_flr = flr_hgt

        return cls(identifier, stories)

    @classmethod
    def from_all_story_geometry(cls, identifier, all_story_geometry,
                                floor_to_floor_heights, perimeter_offset=0,
                                tolerance=0.01):
        """Initialize a Building from an array of Face3Ds arrays representing all floors.

        This method will test to see which of the stories are geometrically unique
        (accoutring for both the floor plate geometry and the floor_to_floor_heights).
        It will only include the unique floor geometries in the resulting Building.

        All of the resulting Room2Ds will have a floor-to-ceiling height equal to the
        Story floor-to-floor height.

        Args:
            identifier: Text string for a unique Building ID. Must be < 100 characters
                and not contain any spaces or special characters.
            all_story_geometry: An array of arrays with each sub-array possessing
                horizontal ladybug-geometry Face3Ds that representing the floor
                plates of the building. Together, these Face3Ds should represent
                all Stories of a building and each array of Face3Ds should together
                represent one Story.
            floor_to_floor_heights: An array of float values with a length equal
                to the number of stories in the Building. Each value in the list
                represents the floor_to_floor height of the Story starting from
                the first floor and then moving to the top floor. Note that numbers
                should be in the units system of the footprint geometry.
            perimeter_offset: An optional positive number that will be used to offset
                the perimeter of the all_story_geometry to create core/perimeter
                zones. If this value is 0, no offset will occur and each story
                will be represented with a single Room2D per polygon (Default: 0).
            tolerance: The maximum difference between x, y, and z values at which
                point vertices are considered to be the same. This is also needed as
                a means to determine which floor geometries are equivalent to one
                another and should be a part the same Story. Default: 0.01, suitable
                for objects in meters.
        """
        # generate the first story of the building
        room_2ds = cls._generate_room_2ds(
            all_story_geometry[0], floor_to_floor_heights[0], perimeter_offset,
            identifier, 1, tolerance)
        stories = [Story('{}_Floor1'.format(identifier), room_2ds,
                         floor_to_floor_heights[0])]

        # generate the remaining unique stories from the floor_to_floor_heights
        remaining_geo = all_story_geometry[1:]
        remaining_flr_hgts = floor_to_floor_heights[1:]
        prev_geo = all_story_geometry[0]
        prev_flr_to_flr = floor_to_floor_heights[0]
        for i, (room_geo, flr_hgt) in enumerate(zip(remaining_geo, remaining_flr_hgts)):
            # test is anything is geometrically different
            if flr_hgt != prev_flr_to_flr or len(room_geo) != len(prev_geo) or \
                    not all(cls._is_story_equivalent(rm1, rm2, tolerance)
                            for rm1, rm2 in zip(room_geo, prev_geo)):
                room_2ds = cls._generate_room_2ds(
                    room_geo, flr_hgt, perimeter_offset, identifier, i + 2, tolerance)
                stories.append(Story(
                    '{}_Floor{}'.format(identifier, i + 2), room_2ds, flr_hgt))
            else:  # geometry is the same as the floor below
                stories[-1].multiplier += 1
            prev_geo = room_geo
            prev_flr_to_flr = flr_hgt

        return cls(identifier, stories)

    @classmethod
    def from_dict(cls, data, tolerance=0, angle_tolerance=0, sort_stories=True):
        """Initialize an Building from a dictionary.

        Args:
            data: A dictionary representation of a Building object.
            tolerance: The maximum difference between z values at which point vertices
                are considered to be in the same horizontal plane. This is used to check
                that all vertices of the input floor_geometry lie in the same horizontal
                floor plane. Default is 0, which will not perform any check.
            angle_tolerance: The max angle difference in degrees that vertices are
                allowed to differ from one another in order to consider them colinear.
                Default is 0, which makes no attempt to evaluate whether the Room
                volume is closed.
            sort_stories: A boolean to note whether the unique_stories should be sorted
                from lowest to highest story upon initialization (True) or whether
                the input order of unique_stories should be left as-is. (Default: True).
        """
        # check the type of dictionary
        assert data['type'] == 'Building', 'Expected Building dictionary. ' \
            'Got {}.'.format(data['type'])
        # extract the 2D Stories
        stories = []
        if 'unique_stories' in data and data['unique_stories'] is not None:
            for s_dict in data['unique_stories']:
                try:
                    stories.append(Story.from_dict(s_dict, tolerance))
                except Exception as e:
                    invalid_dict_error(s_dict, e)
        # extract any additional 3D Rooms
        room_3ds = []
        if 'room_3ds' in data and data['room_3ds'] is not None:
            for r_dict in data['room_3ds']:
                try:
                    room_3ds.append(Room.from_dict(r_dict, tolerance, angle_tolerance))
                except Exception as e:
                    invalid_dict_error(r_dict, e)
        # create the Building object
        building = cls(data['identifier'], stories, room_3ds, sort_stories=sort_stories)
        # assign all other properties that are not a part of initializer
        if 'roof' in data and data['roof'] is not None and 'geometry' in data['roof'] \
                and len(data['roof']['geometry']) > 0:
            roof = RoofSpecification.from_dict(data['roof'])
            building.add_roof_geometry(roof.geometry, tolerance)
        if '_roofs' in data and data['_roofs'] is not None:  # secret for filtered roofs
            bldg_roofs = []
            for st_id, r_spec in data['_roofs']:
                if r_spec is not None:
                    roof = RoofSpecification.from_dict(r_spec)
                    rf_height = (roof.max_height + roof.min_height) / 2
                    bldg_roofs.append((st_id, rf_height, roof))
                else:
                    bldg_roofs.append((st_id, None, None))
            building._roofs = bldg_roofs
        if 'display_name' in data and data['display_name'] is not None:
            building.display_name = data['display_name']
        if 'user_data' in data and data['user_data'] is not None:
            building.user_data = data['user_data']

        if data['properties']['type'] == 'BuildingProperties':
            building.properties._load_extension_attr_from_dict(data['properties'])
        return building

    @classmethod
    def from_honeybee(cls, model, conversion_method='AllRoom2D'):
        """Initialize a Building from a Honeybee Model.

        If each Room has a story, these will be used to determine the separation
        into Dragonfly stories. Otherwise, stories will be auto-generated
        based on the floor heights of rooms.

        Args:
            model: A Honeybee Model to be converted to a Dragonfly Building.
            conversion_method: Text to indicate how to convert the Honeybee Rooms
                that have a loss of information when converted to Dragonfly Room2D.
                Choose from the following options. (Default: AllRoom2D).

                * AllRoom3D - Any Room with Face geometry that is not perfectly
                    horizontal or vertical will be left as-is on Building.room_3ds.
                * ExtrudedOnly - Rooms where all floors are horizontal and walls
                    are vertical will be imported to Room2D. Sloped roof geometries
                    will be imported as Dragonfly RoofSpecification with roof
                    faces being joined across rooms of the same story. Rooms with
                    sloped floors or slanted walls will be left as-is
                    on Building.room_3ds.
                * AllRoom2D - All Honeybee Rooms will be converted to Room2D
                    regardless of the orientation of their Faces. Rooms with
                    sloped floors or slanted walls pointing downwards will be
                    projected into the XY plane to make the Room2D floor plate
                    geometry. Sloped roofs and slanted walls that point upwards
                    will be converted to dragonfly RoofSpecification in an attempt
                    to preserve as much of the original room volume geometry
                    as possible.
        """
        # create the Building object with all rooms as 3D
        dup_rooms = [r.duplicate() for r in model.rooms]
        bldg = cls(model.identifier, room_3ds=dup_rooms)
        bldg._display_name = model._display_name
        # convert the relevant rooms to 2D
        min_diff = parse_distance_string('2m', model.units)
        bldg.convert_all_room_3ds_to_2d(
            conversion_method=conversion_method, min_difference=min_diff,
            tolerance=model.tolerance, angle_tolerance=model.angle_tolerance)
        for story in bldg.unique_stories:
            story._reset_adjacencies_from_honeybee(story.room_2ds, model.tolerance)
        return bldg

    @staticmethod
    def _room_story_geometry_valid(rooms):
        """Check that a set of Honeybee Rooms have geometry that makes a valid Story.

        Args:
            rooms: An array of Honeybee Rooms that will be checked to ensure their
                geometry makes a valid Story.

        Returns:
            True if the Room geometries make a valid Story. False if they do not.
        """
        if len(rooms) == 1:
            return True
        flr_hts = sorted([rm.geometry.min.z for rm in rooms])
        min_flr_to_ceil = min([rm.geometry.max.z - rm.geometry.min.z for rm in rooms])
        return True if flr_hts[-1] - flr_hts[0] < min_flr_to_ceil else False

    @property
    def unique_stories(self):
        """Get a tuple of only unique Story objects that form the Building.

        Repeated stories are represented only once but will have a non-unity multiplier.
        """
        return self._unique_stories

    @unique_stories.setter
    def unique_stories(self, value):
        if value is not None:
            for story in value:
                assert isinstance(story, Story), \
                    'Expected dragonfly Story. Got {}'.format(type(story))
                story._parent = self
            value = tuple(sorted(value, key=lambda x: x.floor_height))
            self._unique_stories = value
        else:
            self._unique_stories = ()
        self._check_geometry_exists()

    @property
    def unique_room_2ds(self):
        """Get a list of the unique Room2D objects that form the Building."""
        rooms = []
        for story in self._unique_stories:
            rooms.extend(story.room_2ds)
        return rooms

    @property
    def room_3ds(self):
        """Get a tuple of additional 3D Honeybee Rooms assigned to the Building.

        These rooms are a part of the Building but are not represented within
        the unique_stories or unique_room_2ds. Matching the Honeybee Room.story
        property to the Dragonfly Story.display_name of an object within the
        unique_stories will effectively place the Honeybee Room on that Story
        for the purposes of floor_area, exterior_wall_area, etc. However, note
        that the Honeybee Room.multiplier property takes precedence over
        whatever multiplier is assigned to the Dragonfly Story that the
        Room.story may reference.
        """
        return self._room_3ds

    @room_3ds.setter
    def room_3ds(self, value):
        if value is not None:
            for room in value:
                assert isinstance(room, Room), \
                    'Expected honeybee Room. Got {}'.format(type(room))
                room._parent = self
            # assign stories to any Rooms that lack them
            if not all([r.story is not None for r in value]):
                Room.stories_by_floor_height(value)
            self._room_3ds = tuple(value)
        else:
            self._room_3ds = ()
        self._check_geometry_exists()

    @property
    def room_3d_faces(self):
        """Get a list of all Face objects for the 3D Honeybee Rooms in the Building."""
        return [face for room in self._room_3ds for face in room._faces]

    @property
    def room_3d_apertures(self):
        """Get a list of all Aperture objects for the 3D Honeybee Rooms in the Building.
        """
        child_apertures = []
        for room in self._room_3ds:
            for face in room._faces:
                child_apertures.extend(face._apertures)
        return child_apertures

    @property
    def room_3d_doors(self):
        """Get a list of all Door objects for the 3D Honeybee Rooms in the Building."""
        child_doors = []
        for room in self._room_3ds:
            for face in room._faces:
                child_doors.extend(face._doors)
        return child_doors

    @property
    def room_3d_shades(self):
        """Get a list of all Shade objects for the 3D Honeybee Rooms in the Building."""
        child_shades = []
        for room in self._room_3ds:
            child_shades.extend(room.shades)
            for face in room.faces:
                child_shades.extend(face.shades)
                for ap in face._apertures:
                    child_shades.extend(ap.shades)
                for dr in face._doors:
                    child_shades.extend(dr.shades)
        return child_shades

    @property
    def has_room_2ds(self):
        """Get a boolean noting whether this Building has Room2Ds assigned under stories.
        """
        return len(self._unique_stories) != 0

    @property
    def has_room_3ds(self):
        """Get a boolean noting whether this Building has 3D Honeybee Rooms.
        """
        return len(self._room_3ds) != 0

    @property
    def has_room_2d_plenums(self):
        """Get a boolean for whether this Building's Room2Ds have plenum depths assigned.
        """
        for story in self._unique_stories:
            if story.has_plenums:
                return True
        return False

    @property
    def room_2d_story_names(self):
        """Get a tuple of all Story display_names that have Room2Ds on them."""
        return tuple(story.display_name for story in self._unique_stories)

    @property
    def room_3d_story_names(self):
        """Get a tuple of all story display_names that have 3D Honeybee Rooms on them."""
        return tuple(set(rm.story for rm in self._room_3ds))

    @property
    def has_zones(self):
        """Get a boolean for whether any Rooms in the Building have zones assigned."""
        return any(room._zone is not None for room in self.unique_room_2ds)

    @property
    def zone_dict(self):
        """Get dictionary of Rooms with zone identifiers as the keys.

        This is useful for grouping rooms by their Zone for export.
        """
        zones = {}
        for room in self.unique_room_2ds:
            try:
                zones[room.zone].append(room)
            except KeyError:  # first room to be found in the zone
                zones[room.zone] = [room]
        return zones

    @property
    def story_count(self):
        """Get an integer for the number of stories in the building.

        This includes both the Room2Ds within unique_stories (including the
        Story.multiplier) as well as all stories defined by the room_3ds.
        """
        r3d_stories = 0
        if self.has_room_3ds:
            story_2ds = self.room_2d_story_names
            for st in self.room_3d_story_names:
                if st not in story_2ds:
                    r3d_stories += 1
        return sum((story.multiplier for story in self._unique_stories)) + r3d_stories

    @property
    def story_count_above_ground(self):
        """Get an integer for the number of stories above the ground.

        All stories defined by 3D Rooms are assumed to be above ground.
        """
        r3d_stories = 0
        if self.has_room_3ds:
            story_2ds = self.room_2d_story_names
            for st in self.room_3d_story_names:
                if st not in story_2ds:
                    r3d_stories += 1
        return sum((story.multiplier for story in self.unique_stories_above_ground)) + \
            r3d_stories

    @property
    def unique_stories_above_ground(self):
        """Get a tuple of unique Story objects that are above the ground.

        A story is considered above the ground if at least one of its Room2Ds
        has an outdoor boundary condition.
        """
        return [story for story in self._unique_stories if story.is_above_ground]

    @property
    def height(self):
        """Get a number for the roof height of the Building as an absolute Z-coordinate.

        This property will account for the fact that the tallest Room may be a 3D
        Honeybee Room.
        """
        r2_h, r3_h = None, None
        if self.has_room_3ds:
            r3_h = max(r.max.z for r in self.room_3ds)
        if self.has_room_2ds:
            last_flr = self._unique_stories[-1]
            r2_h = last_flr.max_room_2d_ceiling_height + \
                (last_flr.floor_to_floor_height * (last_flr.multiplier - 1))
        if r2_h is not None and r3_h is not None:
            return max(r2_h, r3_h)
        elif r2_h is not None:
            return r2_h
        return r3_h

    @property
    def height_above_ground(self):
        """Get a the height difference between the roof and first floor above the ground.

        This property will account for any 3D Room if they exist.
        """
        r2_h, r3_h, bldg_h = None, None, self.height
        try:
            r2_h = bldg_h - self.unique_stories_above_ground[0].min_room_2d_floor_height
        except IndexError:  # building completely below ground or no Room2Ds
            r2_h = 0
        if self.has_room_3ds:
            r3_h = bldg_h - min(r.min.z for r in self.room_3ds)
        if r2_h is not None and r3_h is not None:
            return max(r2_h, r3_h)
        elif r2_h is not None:
            return r2_h
        return r3_h

    @property
    def height_from_first_floor(self):
        """Get a the height difference between the roof and the bottom-most floor.

        This property will account for any 3D Room if they exist.
        """
        r2_h, r3_h, bldg_h = None, None, self.height
        try:
            r2_h = bldg_h - self.unique_stories[0].min_room_2d_floor_height
        except IndexError:  # building completely below ground or no Room2Ds
            r2_h = 0
        if self.has_room_3ds:
            r3_h = bldg_h - min(r.min.z for r in self.room_3ds)
        if r2_h is not None and r3_h is not None:
            return max(r2_h, r3_h)
        elif r2_h is not None:
            return r2_h
        return r3_h

    @property
    def footprint_area(self):
        """Get a number for the total footprint area of the Building.

        The footprint is derived from the lowest dragonfly Story of the building
        unless the Building is composed entirely of 3D Rooms, in which case it
        is the combined floor area of the Rooms belonging to the lowest story.
        """
        try:
            return self._unique_stories[0].floor_area
        except IndexError:  # no Room2Ds
            return sum(r.floor_area for r in self._lowest_story_room_3ds()
                       if not r.exclude_floor_area)

    @property
    def floor_area(self):
        """Get a number for the total floor area in the Building.

        This property uses both the 2D Story multipliers and the 3D Room multipliers
        to determine the total floor area. It will exclude 3D Rooms with a True
        exclude_floor_area and Stories with a True is_plenum.
        """
        fa_r2 = sum([story.floor_area * story.multiplier
                     for story in self._unique_stories if not story.is_plenum])
        fa_r3 = sum([room.floor_area * room.multiplier for room in self._room_3ds
                     if not room.exclude_floor_area])
        return fa_r2 + fa_r3

    @property
    def exterior_wall_area(self):
        """Get a number for the total exterior wall area in the Building.

        This property uses both the 2D Story multipliers and the 3D Room multipliers
        to determine the total exterior wall area.
        """
        ewa_r2 = sum([story.exterior_wall_area * story.multiplier
                      for story in self._unique_stories])
        ewa_r3 = sum([r.exterior_wall_area * r.multiplier for r in self._room_3ds])
        return ewa_r2 + ewa_r3

    @property
    def exterior_aperture_area(self):
        """Get a number for the total exterior wall aperture area in the Building.

        This property uses both the 2D Story multipliers and the 3D Room multipliers
        to determine the total exterior wall aperture area. All skylights apertures
        are excluded.
        """
        eaa_r2 = sum([story.exterior_aperture_area * story.multiplier
                      for story in self._unique_stories])
        eaa_r3 = sum([room.exterior_wall_aperture_area * room.multiplier
                      for room in self._room_3ds])
        return eaa_r2 + eaa_r3

    @property
    def volume(self):
        """Get a number for the volume of all the Rooms in the Building.

        This property uses both the 2D Story multipliers and the 3D Room multipliers
        to determine the total Building volume.
        """
        v_2r = sum([story.volume * story.multiplier for story in self._unique_stories])
        v_3r = sum([room.volume * room.multiplier for room in self._room_3ds])
        return v_2r + v_3r

    @property
    def min(self):
        """Get a Point2D for the min bounding rectangle vertex in the XY plane.

        This is useful in calculations to determine if this Building is in proximity
        to other objects.
        """
        r2_min_pt, r3_min_pt = None, None
        if self.has_room_2ds:
            r2_min_pt = self._calculate_min(self._unique_stories)
        if self.has_room_3ds:
            r3_min_pt = Model._calculate_min(self._room_3ds)
        if r2_min_pt is not None and r3_min_pt is not None:
            return Point2D(min(r2_min_pt.x, r3_min_pt.x), min(r2_min_pt.y, r3_min_pt.y))
        elif r2_min_pt is not None:
            return r2_min_pt
        return Point2D(r3_min_pt.x, r3_min_pt.y)

    @property
    def max(self):
        """Get a Point2D for the max bounding rectangle vertex in the XY plane.

        This is useful in calculations to determine if this Building is in proximity
        to other objects.
        """
        r2_max_pt, r3_max_pt = None, None
        if self.has_room_2ds:
            r2_max_pt = self._calculate_max(self._unique_stories)
        if self.has_room_3ds:
            r3_max_pt = Model._calculate_max(self._room_3ds)
        if r2_max_pt is not None and r3_max_pt is not None:
            return Point2D(max(r2_max_pt.x, r3_max_pt.x), max(r2_max_pt.y, r3_max_pt.y))
        elif r2_max_pt is not None:
            return r2_max_pt
        return Point2D(r3_max_pt.x, r3_max_pt.y)

    def all_stories(self):
        """Get a list of all Story objects that form the Building.

        The Story objects returned here each have a multiplier of 1 and repeated
        stories are represented will their own Story object. 3D Rooms are not included
        in this output.
        """
        all_stories = []
        for story in self._unique_stories:
            new_story = story.duplicate()
            new_story.multiplier = 1
            all_stories.append(new_story)

            if story.multiplier != 1:
                for i in range(story.multiplier - 1):
                    new_story = story.duplicate()
                    new_story.add_prefix('Flr{}'.format(i + 1))
                    new_story.multiplier = 1
                    m_vec = Vector3D(0, 0, story.floor_to_floor_height * (i + 1))
                    new_story.move(m_vec)
                    all_stories.append(new_story)
        return all_stories

    def all_room_2ds(self):
        """Get a list of all Room2D objects that form the Building."""
        rooms = []
        for story in self.all_stories():
            rooms.extend(story.room_2ds)
        return rooms

    def room_2ds_by_display_name(self, room_name):
        """Get all of the Room2Ds with a given display_name in the Building."""
        rooms = []
        for room in self.unique_room_2ds:
            if room.display_name == room_name:
                rooms.append(room)
        return rooms

    def room_3ds_by_display_name(self, room_name):
        """Get all of the 3D Rooms with a given display_name in the Building."""
        rooms = []
        for room in self.room_3ds:
            if room.display_name == room_name:
                rooms.append(room)
        return rooms

    def room_3ds_by_story(self, story_name):
        """Get all of the 3D Honeybee Room objects assigned to a particular story.

        Args:
            story_name: Text for the display_name of the Story for which
                Honeybee Room objects will be returned.
        """
        rooms = []
        for room in self.room_3ds:
            if room.story == story_name:
                rooms.append(room)
        return rooms

    def footprint(self, tolerance=0.01):
        """A list of Face3D objects representing the footprint of the building.

        The footprint is derived from the lowest story of the building and, if
        all Room2Ds of this story can be joined into a single continuous polyface,
        then only one Face3D will be contained in the list output from this method.
        Otherwise, several Face3Ds may be output.

        Args:
            tolerance: The minimum distance between points at which they are
                not considered touching. Default: 0.01, suitable for objects
                in meters.
        """
        if self.has_room_2ds:
            ground_story = self._unique_stories[0]
            if len(ground_story.room_2ds) == 1:  # no need to create any new geometry
                return [ground_story.room_2ds[0].floor_geometry]
            else:  # need a single list of Face3Ds for the whole footprint
                return ground_story.footprint(tolerance)
        foot_rooms = self._lowest_story_room_3ds()
        return Room.grouped_horizontal_boundary(foot_rooms, tolerance=tolerance)

    def shade_representation(
            self, exclude_index=None, cap=False, include_room3ds=False, tolerance=0.01):
        """A list of honeybee Shade objects representing the building geometry.

        These can be used to account for this Building's shade in the simulation of
        another nearby Building.

        Args:
            exclude_index: An optional index for a unique_story to be excluded from
                the shade representation. If None, all stories will be included
                in the result. (Default: None).
            cap: Boolean to note whether the shade representation should be capped
                with a top face. Usually, this is not necessary to account for
                blocked sun and is only needed when it's important to account for
                reflected sun off of roofs. (Default: False).
            include_room3ds: Boolean to note whether the 3D Rooms assigned to
                this Building should be included in the shade representation.
                Only exterior geometries are included. (Default: False).
            tolerance: The minimum distance between points at which they are
                not considered touching. Default: 0.01, suitable for objects
                in meters.
        """
        context_shades = []
        if exclude_index is None:
            for story in self.unique_stories:
                if not story.is_plenum:
                    context_shades.extend(story.shade_representation(cap, tolerance))
        else:
            for i, story in enumerate(self.unique_stories):
                if not story.is_plenum:
                    if i != exclude_index:
                        context_shades.extend(story.shade_representation(cap, tolerance))
                    else:
                        mult_shd = story.shade_representation_multiplier(
                            cap=cap, tolerance=tolerance)
                        context_shades.extend(mult_shd)
        if include_room3ds and self.has_room_3ds:
            for room in self.room_3ds:
                for face in room.faces:
                    if isinstance(face.boundary_condition, Outdoors):
                        context_shades.append(Shade(face.identifier, face.geometry))
        return context_shades

    def suggested_alignment_axes(
            self, distance, direction=Vector2D(0, 1), angle_tolerance=1.0):
        """Get suggested LineSegment2Ds to be used for this Building.

        This method will return the most common axes across the Building's Room2D
        geometry along with the number of Room2D segments that correspond to each axis.
        The latter can be used to filter the suggested alignment axes to get
        only the most common ones across the Building.

        Args:
            distance: A number for the distance that will be used in the alignment
                operation. This will be used to determine the resolution at which
                alignment axes are generated and evaluated. Smaller alignment
                distances will result in the generation of more common_axes since
                a finer resolution can differentiate common that would typically be
                grouped together. For typical building geometry, an alignment distance
                of 0.3 meters or 1 foot is typically suitable for eliminating
                unwanted details while not changing the geometry too much from
                its original location.
            direction: A Vector2D object to represent the direction in which the
                common axes will be evaluated and generated.
            angle_tolerance: The max angle difference in radians that the Room2D
                segment direction can differ from the input direction before the
                segments are not factored into this calculation of common axes.

            Returns:
                A tuple with two elements.

            -   common_axes: A list of LineSegment2D objects for the common
                axes across the input Room2Ds.

            -   axis_values: A list of integers that aligns with the common_axes
                and denotes how many segments of the input Room2D each axis
                relates to. Higher numbers indicate that that the axis is more
                commonly aligned across the Room2Ds.
        """
        return Room2D.generate_alignment_axes(
            self.unique_room_2ds, distance, direction, angle_tolerance)

    def find_adjacency_gaps(self, gap_distance=0.1, tolerance=0.01):
        """Identify gaps smaller than a gap_distance between this Building's Room2Ds.

        All cases where gaps can create failed adjacency solving or failed
        intersections between adjacent stories will be checked.

        Args:
            gap_distance: The maximum distance between two Room2Ds that is considered
                an adjacency gap. Differences between Room2Ds that are higher than
                this distance are considered meaningful gaps to be preserved.
                This value should be higher than the tolerance to be
                meaningful. (Default: 0.1, suitable for objects in meters).
            tolerance: The minimum difference between the coordinate values at
                which point they are considered equivalent. (Default: 0.01,
                suitable for objects in meters).

        Returns:
            A list of Point2Ds that note the location of any gaps between this
            Building's  Room2Ds, which are larger than the tolerance but less
            than the gap_distance.
        """
        gap_points = []
        prev_mult, story_count = 0, len(self._unique_stories)
        for i, story in enumerate(self._unique_stories):
            if prev_mult == 1:  # test this story together with the one below
                room_group = story.room_2ds + self._unique_stories[i - 1].room_2ds
                pts = Room2D.find_adjacency_gaps(room_group, gap_distance, tolerance)
                gap_points.extend(pts)
            elif story.multiplier != 1:  # lone bottom/middle story to test
                pts = Room2D.find_adjacency_gaps(story.room_2ds, gap_distance, tolerance)
                gap_points.extend(pts)
            elif i + 1 == story_count:  # lone top story to test
                pts = Room2D.find_adjacency_gaps(story.room_2ds, gap_distance, tolerance)
                gap_points.extend(pts)
            prev_mult = story.multiplier
        return list(set(gap_points))  # remove duplicates in the result

    def convert_multipliers_to_stories(self):
        """Convert this Building's stories with non-unity multipliers to geometry."""
        exist_story_ids = set(story.identifier for story in self.unique_stories)
        stories_to_add = []
        for story in self.all_stories():
            if story.identifier not in exist_story_ids:
                stories_to_add.append(story)
        self.add_stories(stories_to_add)
        for story in self.unique_stories:
            story.multiplier = 1

    def add_stories(self, stories, add_duplicate_ids=False):
        """Add additional Story objects to this Building.

        Using this method will ensure that Stories are ordered according to their
        floor height as they are added. Also, in the case that Story identifiers
        match an existing one in this Building, these Stories will be merged
        together. If add_duplicate_ids is False, Room2Ds that have matching
        identifiers within a merged Story will not be ignored in order to
        avoid ID conflicts.

        Args:
            stories: A list or tuple of Story objects to be added to this Building.
            add_duplicate_ids: A boolean to note whether added Room2Ds that
                have matching identifiers within each Story should be ignored (False)
                or they should be added to the Story creating an ID collision
                that can be resolved later (True). (Default: False).
        """
        # check to be sure all of the input is correct
        for story in stories:
            assert isinstance(story, Story), \
                'Expected dragonfly Story. Got {}'.format(type(story))
        # create the list of new stories, merging stories that have the same identifier
        new_stories = list(self._unique_stories)
        for o_story in stories:
            for e_story in new_stories:
                if o_story.identifier == e_story.identifier:
                    e_story.add_room_2ds(o_story.room_2ds, add_duplicate_ids)
                    break
            else:
                o_story._parent = self
                new_stories.append(o_story)
        # sort the stories by floor level and assign them to this Building
        unique_stories = tuple(sorted(new_stories, key=lambda x: x.floor_height))
        self._unique_stories = unique_stories

    def add_room_3ds(self, rooms, add_duplicate_ids=False):
        """Add additional 3D Honeybee Room objects to this Building.

        Args:
            stories: A list or tuple of Honeybee Room objects to be added to
                this building.
            add_duplicate_ids: A boolean to note whether added Rooms that
                have matching identifiers within the current Building should be
                ignored (False) or they should be added to the Building creating
                an ID collision that can be resolved later (True). (Default: False).
        """
        # check to be sure that the input is composed of Rooms
        for room in rooms:
            assert isinstance(room, Room), \
                'Expected honeybee Room. Got {}'.format(type(room))
        # add the rooms and deal with duplicated IDs appropriately
        new_room_3ds = list(self._room_3ds)
        if add_duplicate_ids:
            for room in rooms:
                room._parent = self
                if room.story is None:
                    room.story = 'Unknown'
                new_room_3ds.append(room)
        else:
            exist_set = {rm.identifier for rm in self._room_3ds}
            for room in rooms:
                if room.identifier not in exist_set:
                    room._parent = self
                    if room.story is None:
                        room.story = 'Unknown'
                    new_room_3ds.append(room)
        # assign the new Rooms to this Building
        self._room_3ds = tuple(new_room_3ds)

    def add_roof_geometry(self, roof_geometry, tolerance=0.01, overlap_threshold=0):
        """Add roof geometry to the stories of this Building.

        This method will attempt add each roof geometry to the best Story in the
        Building by checking for overlaps between the Story's Room2Ds and the
        Roof geometry in plan. When a given roof geometry overlaps with several
        Stories more than the specified overlap_threshold, the top-most Story
        will get the roof geometry assigned to it unless this top Story has a
        floor_height above the roof geometry, in which case the next highest story
        will be checked until a compatible one is found. If a given roof geometry
        does not overlap with any story geometry or lies below all of the stories,
        it will not be assigned to the Building.

        Args:
            roof_geometry: An array of Face3D objects representing the geometry
                of the Roof.
            tolerance: The maximum difference between values at which point vertices
                are considered to be the same. (Default: 0.01, suitable for
                objects in Meters).
            overlap_threshold: A number between 0 and 1 for the fraction of a room's
                area that must be covered by a given roof geometry for it to be
                considered overlapping with that room. This is intended to prevent
                incorrect roof assignment in cases where roofs extend slightly
                past the room they are intended for. (Default: 0.05).
        """
        # convert all roof geometries to clean 2D polygons
        roof_polygons, clean_roofs = [], []
        for r_geo in roof_geometry:
            try:
                clean_geo = r_geo.remove_colinear_vertices(tolerance)
            except AssertionError:  # degenerate roof geometry to ignore
                continue
            clean_poly = Polygon2D(tuple(Point2D(pt.x, pt.y) for pt in r_geo.boundary))
            clean_roofs.append(clean_geo)
            roof_polygons.append(clean_poly)
        roof_geometry = clean_roofs
        if len(roof_geometry) == 0:
            return

        # prepare the stories for checking the roofs
        rev_stories = list(reversed(self.unique_stories))
        story_polygons, story_heights, room_heights = [], [], []
        for story in rev_stories:
            room_polygons = tuple(rm.floor_geometry.polygon2d for rm in story.room_2ds)
            rm_heights = tuple(rm.floor_height for rm in story.room_2ds)
            story_polygons.append(room_polygons)
            story_heights.append(story.floor_height)
            room_heights.append(rm_heights)

        # loop through the roof_geometry and find a compatible story
        proj_dir = Vector3D(0, 0, 1)
        ot = overlap_threshold
        story_roofs = [[] for _ in rev_stories]  # holds geo assigned to each story
        for rf_geo, rf_poly in zip(roof_geometry, roof_polygons):
            zip_obj = zip(story_heights, story_polygons, room_heights)
            for i, (st_ht, story_poly, rm_hts) in enumerate(zip_obj):
                if rf_geo.max.z < st_ht:
                    continue  # roof completely below story; valid assignment impossible
                overlaps_story = False
                for rm_poly, rm_ht in zip(story_poly, rm_hts):
                    poly_rel = rf_poly.polygon_relationship(rm_poly, tolerance)
                    if poly_rel >= 0:
                        try:
                            rm_poly = rm_poly.remove_colinear_vertices(tolerance)
                        except AssertionError:  # degenerate room to ignore
                            continue
                        try:
                            overlap_polys = rf_poly.boolean_intersect(rm_poly, tolerance) \
                                if poly_rel == 0 else [rm_poly]
                        except Exception:
                            continue  # not considered a significant overlap
                        if sum(ply.area for ply in overlap_polys) < rm_poly.area * ot:
                            continue  # not considered a significant overlap
                        plane_ints = []
                        for ov_poly in overlap_polys:
                            for pt in ov_poly:
                                b_pt = Point3D(pt.x, pt.y, rm_ht)
                                r_ray = Ray3D(b_pt, proj_dir)
                                pi = rf_geo.plane.intersect_line_ray(r_ray)
                                if pi is not None:
                                    plane_ints.append(pi)
                                elif rf_geo.plane.distance_to_point(b_pt) <= tolerance:
                                    plane_ints.append(b_pt)
                                else:
                                    plane_ints.append(None)
                        if all(pi is not None for pi in plane_ints):
                            overlaps_story = True
                        else:  # roof extends below room; valid assignment impossible
                            overlaps_story = False
                            break
                if overlaps_story:  # we have found the story to assign the roof geometry
                    story_roofs[i].append(rf_geo)
                    break

        # create the RoofSpecification objects and assign them to the stories
        for story, roof_geos in zip(rev_stories, story_roofs):
            if len(roof_geos) != 0:
                if story.roof is not None:  # combine the existing roof with the new one
                    new_roof = RoofSpecification(story.roof.geometry + tuple(roof_geos))
                else:
                    new_roof = RoofSpecification(roof_geos)
                story.roof = new_roof

    def remove_duplicate_roofs(self, tolerance=0.01):
        """Remove any roof geometries in the Building that appear more than once.

        This includes duplicated roof geometries assigned to different stories.

        Args:
            tolerance: The maximum difference between values at which point vertices
                are considered to be the same. (Default: 0.01, suitable for
                objects in Meters).
        """
        # collect all roof geometries across all stories
        roof_geos = []
        for story in self.unique_stories:
            if story.roof is not None:
                roof_geos.extend(story.roof.geometry)
                story.roof = None
        # remove duplicate geometries from the list
        clean_roof_geo = []
        for r_geo in roof_geos:
            for exist_geo in clean_roof_geo:
                if r_geo.is_geometrically_equivalent(exist_geo, tolerance):
                    break  # duplicate geometry found
            else:  # the geometry is not yet in the clean list
                clean_roof_geo.append(r_geo)
        # re-assign the roof geometry to the stories
        self.add_roof_geometry(clean_roof_geo, tolerance)

    def convert_room_3d_to_2d(self, room_3d_identifier, tolerance=0.01):
        """Convert a single 3D Honeybee Room to a Dragonfly Room2D on this Building.

        This process will add the Room2D to an existing Dragonfly Story on the
        Building if the Honeybee Room.story matches a Story.display_name on this
        object. If not, a new Story on this Building will be initialized.

        Args:
            room_3d_identifier: The identifier of the 3D honeybee Room on this
                Building that will be converted to a dragonfly Room2D.
            tolerance: The maximum difference between values at which point vertices
                are considered to be the same. (Default: 0.01, suitable for
                objects in Meters).

        Returns:
            The newly-created Room2D object from the converted Room. Will be
            None if the Honeybee Room is not a closed solid and cannot be
            converted to a valid Room2D.
        """
        # get the Honeybee Room object to be converted
        hb_room_i = [i for i, r in enumerate(self.room_3ds)
                     if r.identifier == room_3d_identifier]
        if len(hb_room_i) == 0:
            raise ValueError(
                'No 3D Honeybee Room with an identifier of "{}" was found on '
                'Building "{}"'.format(room_3d_identifier, self.display_name))
        elif len(hb_room_i) != 1:
            raise ValueError(
                'Multiple 3D Honeybee Rooms with an identifier of "{}" were found on '
                'Building "{}"'.format(room_3d_identifier, self.display_name))
        new_room_3ds = list(self._room_3ds)
        hb_room = new_room_3ds.pop(hb_room_i[0])
        # create a Dragonfly Room2D from the Honeybee Room
        try:
            df_room = Room2D.from_honeybee(hb_room, tolerance)
        except Exception:  # room is not a closed solid
            return None
        self._room_3ds = tuple(new_room_3ds)
        # assign the Room2D to an existing Story or create a new one
        for story in self._unique_stories:
            if story.display_name == hb_room.story:
                story.add_room_2d(df_room)
                break
        else:  # a new Story object has to be initialized
            new_story = Story(clean_string(hb_room.story), (df_room,))
            new_story.display_name = hb_room.story
            self.add_stories([new_story])
        return df_room

    def convert_room_3ds_to_2d(self, room_3d_identifiers, tolerance=0.01):
        """Convert several 3D Honeybee Rooms on this Building to a Dragonfly Room2Ds.

        This process will add the Room2Ds to an existing Dragonfly Story on the
        Building if the Honeybee Room.story matches a Story.display_name on this
        object. If not, a new Story on this Building will be initialized.

        Args:
            room_3d_identifiers: A list of the identifiers for the 3D honeybee
                Rooms on this Building that will be converted to dragonfly Room2Ds.
            tolerance: The maximum difference between values at which point vertices
                are considered to be the same. (Default: 0.01, suitable for
                objects in Meters).

        Returns:
            A list of the newly-created Room2D objects from the converted Rooms.
            If a given 3D Room is not valid and cannot be converted to a Room2D,
            it will not be included in this output.
        """
        df_rooms = []
        for r3_id in room_3d_identifiers:
            new_r2 = self.convert_room_3d_to_2d(r3_id, tolerance)
            if new_r2 is not None:
                df_rooms.append(new_r2)
        return df_rooms

    def convert_all_room_3ds_to_2d(
            self, conversion_method='AllRoom2D', min_difference=2.0,
            tolerance=0.01, angle_tolerance=1):
        """Convert all 3D Honeybee Rooms on this Building to a Dragonfly Room2Ds.

        This process will add the Room2Ds to an existing Dragonfly Story on the
        Building if the Honeybee Room.story matches a Story.display_name on this
        object. If not, a new Story on this Building will be initialized.

        Args:
            conversion_method: Text to indicate how to convert the 3D Rooms
                that have a loss of information when converted to Dragonfly Room2D.
                Choose from the following options. (Default: AllRoom2D).

                * AllRoom3D - Any Room with Face geometry that is not perfectly
                    horizontal or vertical will be left as-is on Building.room_3ds.
                * ExtrudedOnly - Rooms where all floors are horizontal and walls
                    are vertical will be converted to Room2D. Sloped roof geometries
                    will be converted to Dragonfly RoofSpecification with roof
                    faces being joined across rooms of the same story. Rooms with
                    sloped floors or slanted walls will be left as-is
                    on Building.room_3ds.
                * AllRoom2D - All Honeybee Rooms will be converted to Room2D
                    regardless of the orientation of their Faces. Rooms with
                    sloped floors or slanted walls pointing downwards will be
                    projected into the XY plane to make the Room2D floor plate
                    geometry. Sloped roofs and slanted walls that point upwards
                    will be converted to dragonfly RoofSpecification in an attempt
                    to preserve as much of the original room volume geometry
                    as possible.

            min_difference:  An float value to denote the minimum difference
                in floor heights that is considered meaningful. This is used to
                establish stories in the event that the 3D Rooms do not already
                have them. (Default: 2.0, which is suitable for models in meters).
            tolerance: The maximum difference between values at which point vertices
                are considered to be the same. (Default: 0.01, suitable for
                objects in Meters).
            angle_tolerance: The max angle difference in degrees that Face3D normals
                are allowed to differ from the vertical or horizontal before they
                are no longer considered as such. (Default: 1 degree).

        Returns:
            A list of the newly-created Room2D objects from the converted Rooms.
        """
        # verify that the input method is valid
        method = conversion_method.lower()
        if method not in ('allroom3d', 'extrudedonly', 'allroom2d'):
            msg = 'Building conversion_method "{}" is not recognized\nChoose from: ' \
                'AllRoom2D, ExtrudedOnly, AllRoom3D.'.format(conversion_method)
            raise ValueError(msg)

        # collect the relevant 3D Rooms to be converted
        new_room_3ds, hb_rooms = [], []
        if method == 'allroom3d':
            for hb_room in self.room_3ds:
                if self._is_room_flat_top_extrusion(hb_room, tolerance, angle_tolerance):
                    hb_rooms.append(hb_room)
                else:
                    new_room_3ds.append(hb_room)
        elif method == 'extrudedonly':
            for hb_room in self.room_3ds:
                if self._is_room_sloped_extrusion(hb_room, tolerance, angle_tolerance):
                    hb_rooms.append(hb_room)
                else:
                    new_room_3ds.append(hb_room)
        else:
            hb_rooms = self.room_3ds

        # assign stories if they don't already exist
        if not all([room.story is not None for room in hb_rooms]):
            Room.stories_by_floor_height(hb_rooms.min_difference)

        # group the rooms by story and create dragonfly Stories
        story_dict = {}
        for room in hb_rooms:
            try:
                story_dict[room.story].append(room)
            except KeyError:
                story_dict[room.story] = [room]

        # evaluate floor heights to see if stories should be split
        removed_flrs, new_flrs = [], {}
        for s_id, rms in story_dict.items():
            if not self._room_story_geometry_valid(rms):
                rm_grps, flr_hts = Room.group_by_floor_height(rms, min_difference)
                for grp, ht in zip(rm_grps, flr_hts):
                    new_flrs['{}_{}'.format(s_id, ht)] = grp
                removed_flrs.append(s_id)
        for r_flr in removed_flrs:
            story_dict.pop(r_flr)
        story_dict.update(new_flrs)
        for s_id, rms in story_dict.items():
            for rm in rms:
                rm.story = s_id

        # convert the relevant 3D Rooms to Room2D
        df_rooms, roof_dict = [], {}
        for hb_room in hb_rooms:
            # create a Dragonfly Room2D from the Honeybee Room
            try:
                df_room = Room2D.from_honeybee(hb_room, tolerance)
            except Exception:  # invalid Honeybee Room that is not a closed solid
                new_room_3ds.append(hb_room)
                continue
            # extract the relevant roof and skylight geometries
            rfs, skylights = [], []
            if method == 'extrudedonly':
                for face in hb_room.roof_ceilings:
                    if face.tilt > angle_tolerance:
                        rfs.append(face.geometry)
            elif method == 'allroom2d':
                for face in hb_room.faces:
                    if angle_tolerance < face.tilt < 90 - angle_tolerance:
                        rfs.append(face.geometry)
                        if len(face.sub_faces) != 0 and isinstance(face.type, Wall):
                            skylights.append(
                                DetailedSkylights.from_honeybee(face.sub_faces))
            # assign the roof geometries to a dictionary to keep track of them
            if len(rfs) != 0:
                try:
                    roof_dict[hb_room.story].extend(rfs)
                except KeyError:
                    roof_dict[hb_room.story] = rfs
            # assign any skylights to the Room2D
            if len(skylights) != 0:
                if len(skylights) == 1:
                    new_sky_light = skylights[0]
                else:
                    new_polys = skylights[0].polygons
                    new_is_dr = skylights[0].are_doors
                    for sl in skylights[1:]:
                        new_polys += sl.polygons
                        new_is_dr += sl.are_doors
                    new_sky_light = DetailedSkylights(new_polys, new_is_dr)
                df_room.skylight_parameters = new_sky_light
            # assign the Room2D to an existing Story or create a new one
            for story in self._unique_stories:
                if story.display_name == hb_room.story:
                    story.add_room_2d(df_room)
                    break
            else:  # a new Story object has to be initialized
                new_story = Story(clean_string(hb_room.story), (df_room,))
                new_story.display_name = hb_room.story
                self.add_stories([new_story])
            df_rooms.append(df_room)

        # build roof specifications from joined versions of the roofs
        for story_name, roof_geos in roof_dict.items():
            for story in self._unique_stories:
                if story.display_name == story_name:
                    all_geo = story.roof.geometry + tuple(roof_geos) \
                        if story.roof is not None else roof_geos
                    story.roof = RoofSpecification.from_geometry_to_join(
                        all_geo, tolerance)
                    break

        # reset the 3D Rooms on this object
        self._room_3ds = tuple(new_room_3ds)
        return df_rooms

    def add_prefix(self, prefix):
        """Change the object identifier and all child objects by inserting a prefix.

        This is particularly useful in workflows where you duplicate and edit
        a starting object and then want to combine it with the original object
        into one Model (like making a model of repeating buildings) since all objects
        within a Model must have unique identifiers.

        Args:
            prefix: Text that will be inserted at the start of this object's
                (and child objects') identifier and display_name. It is recommended
                that this prefix be short to avoid maxing out the 100 allowable
                characters for dragonfly identifiers.
        """
        self.identifier = clean_string('{}_{}'.format(prefix, self.identifier))
        if self._display_name is not None:
            self.display_name = '{}_{}'.format(prefix, self.display_name)
        self.properties.add_prefix(prefix)
        for story in self.unique_stories:
            story.add_prefix(prefix)
        for room in self.room_3ds:
            room.add_prefix(prefix)

    def automatically_zone(self, orient_count=None, north_vector=Vector2D(0, 1),
                           attr_name=None):
        """Automatically group the rooms of this Building into zones.

        Relevant properties that are used to group Room2Ds into zones include story,
        orientation, and additional attributes (like programs).

        Args:
            orient_count: An optional positive integer to set the number of orientation
                groups to use for zoning. For example, setting this to 4 will result
                in zones being established based on the four orientations (North,
                East, South, West). If None, the maximum number of unique groups
                will be used.
            north_vector: A ladybug_geometry Vector2D for the north direction.
                Default is the Y-axis (0, 1).
            attr_name: A string of an attribute that the input Room2Ds should have.
                This can have '.' that separate the nested attributes from one another.
                For example, 'properties.energy.program_type'.
        """
        for story in self.unique_stories:
            story.automatically_zone(orient_count, north_vector, attr_name)
        if len(self.room_3ds) != 0:
            Room.automatically_zone(self.room_3ds, orient_count, north_vector, attr_name)

    def sort_stories(self):
        """Sort the stories assigned to this Building by their floor heights"""
        self._unique_stories = \
            tuple(sorted(self._unique_stories, key=lambda x: x.floor_height))

    def separate_top_bottom_floors(self):
        """Separate top/bottom Stories with non-unity multipliers into their own Stories.

        The resulting first and last Stories will each have a multiplier of 1 and
        duplicated middle Stories will be added as needed. This method also
        automatically assigns the first story Room2Ds to have a ground contact
        floor and the top story Room2Ds to have an outdoor-exposed roof.

        This is particularly helpful when using to_honeybee workflows with
        multipliers but one wants to account for the heat exchange of the top
        or bottom floors with the ground or outdoors.
        """
        # do not do anything if the Building has no 2D Stories
        if not self.has_room_2ds:
            return

        # empty tuples in case no floors are added
        new_ground_floor, new_top_floor = (), ()

        # ensure that the bottom floor is unique
        if self._unique_stories[0].multiplier != 1:
            story = self._unique_stories[0]
            new_ground_floor = (self._separated_ground_floor(story),)
            story.multiplier = story.multiplier - 1
            story.move(Vector3D(0, 0, story.floor_to_floor_height))  # 2nd floor

        # ensure that the top floor is unique
        if self._unique_stories[-1].multiplier != 1:
            story = self._unique_stories[-1]
            new_top_floor = (self._separated_top_floor(story),)
            story.multiplier = story.multiplier - 1

        # set the unique stories to include any new top and bottom floors
        self._unique_stories = new_ground_floor + self._unique_stories + new_top_floor

        # assign the is_ground_contact and is_top_exposed properties
        self._unique_stories[0].set_ground_contact()
        self._unique_stories[-1].set_top_exposed()

    def separate_mid_floors(self, tolerance=0.01):
        """Separate all Stories with non-unity multipliers into two or three Stories.

        This method automatically assigns the first story Room2Ds to have a ground
        contact floor and will separate the top story of each unique story to
        have outdoor-exposed roofs when no Room2Ds are sensed above a given room.

        This is particularly helpful when using to_honeybee workflows with
        multipliers but one wants to account for the heat exchange of the top
        or bottom floors with the ground or outdoors.

        Args:
            tolerance: The tolerance that will be used to compute the point within
                the floor boundary that is used to check whether there is geometry
                above each Room2D. It is recommended that this number not be less
                than 1 centimeter to avoid long computation times. Default: 0.01,
                suitable for objects in meters.
        """
        # do not do anything if the Building has no 2D Stories
        if not self.has_room_2ds:
            return

        # ensure that the bottom floor is unique
        if self._unique_stories[0].multiplier != 1:
            story = self._unique_stories[0]
            new_ground_floor = self._separated_ground_floor(story)
            story.multiplier = story.multiplier - 1
            story.move(Vector3D(0, 0, story.floor_to_floor_height))  # 2nd floor
        else:
            new_ground_floor = self._unique_stories[0]
            if len(self._unique_stories) > 1:
                new_ground_floor.set_top_exposed_by_story_above(
                    self._unique_stories[1], tolerance)
            self._unique_stories = self._unique_stories[1:]

        # ensure that the top floor is unique
        new_top_floors = []
        for i, story in enumerate(self._unique_stories):
            if story.multiplier != 1:
                new_top_floor = self._separated_top_floor(story)
                story.multiplier = story.multiplier - 1
                try:
                    new_top_floor.set_top_exposed_by_story_above(
                        self._unique_stories[i + 1], tolerance)
                except IndexError:  # this is the last story
                    new_top_floor.set_top_exposed()
                new_top_floors.extend((story, new_top_floor))
            else:
                if i == len(self._unique_stories) - 1:
                    story.set_top_exposed()
                else:
                    story.set_top_exposed_by_story_above(
                        self._unique_stories[i + 1], tolerance)
                new_top_floors.append(story)

        # set the unique stories to include any new top and bottom floors
        self._unique_stories = (new_ground_floor,) + tuple(new_top_floors)

        # assign the is_ground_contact and is_top_exposed properties
        self._unique_stories[0].set_ground_contact()

    def make_basement_stories(self, basement_count=1, remove_windows=False,
                              tolerance=0.01):
        """Make the lowest unique Story(s) of this Building into basements.

        This involves setting the outdoor walls of the basement stories to all
        have ground boundary conditions and setting the is_ground_contact
        property on all relevant Room2Ds that are a basement or have a basement
        story below them.

        Args:
            basement_count: A positive integer for the number of unique Stories
                on this Building to make into basements. (Default: 1).
            remove_windows: Boolean to note whether basement Room2D segments
                with windows should have their outdoor boundary conditions and
                windows kept (True) or whether the windows should be removed
                in order to assign a ground boundary condition to all
                walls (False). (Default: False).
            tolerance: The tolerance that will be used to compute the point within
                the floor boundary that is used to check whether there is geometry
                below each Room2D. It is recommended that this number not be less
                than 1 centimeter to avoid long computation times. Default: 0.01,
                suitable for objects in meters.
        """
        # check that the basement count is appropriate
        if basement_count <= 0:
            return
        if basement_count > len(self._unique_stories):
            basement_count = len(self._unique_stories)
        # assign underground walls to all basement stories
        for story in self._unique_stories[:basement_count]:
            story.make_underground(remove_windows)
        # set the ground contact property for basement Room2Ds
        self._unique_stories[0].set_ground_contact()
        max_gnd_count = basement_count + 1 \
            if basement_count < len(self._unique_stories) else len(self._unique_stories)
        if len(self._unique_stories) != 1:
            for i, story in enumerate(self._unique_stories[1:max_gnd_count]):
                story_below = self._unique_stories[i]
                story.set_ground_contact_by_story_below(story_below, tolerance)

    def split_room_2d_vertically(self, room_id, tolerance=0.01):
        """Split a Room2D in this Building vertically if it crosses multiple stories.

        Args:
            room_id: The identifier of a Room2D within this Building which will
                be split vertically with the Stories above it.
            tolerance: The tolerance to be used for determining whether the Room2D
                should be split. Default: 0.01, suitable for objects in meters.

        Returns:
            A list of all the new rooms created by running the method. This
            can be used to post-process the rooms for attributes like adjacency
            within the Story they are placed.
        """
        # loop through the stories of the model and find the Room2D
        found_room, split_heights, split_stories = None, [], []
        for story in self._unique_stories:
            if found_room is not None:
                flr_hgt = story.median_room2d_floor_height
                if found_room.ceiling_height - tolerance > flr_hgt:
                    split_heights.append(flr_hgt)
                    split_stories.append(story)
            else:
                for rm in story.room_2ds:
                    if rm.identifier == room_id:
                        found_room = rm
                        break

        # check if the room was found and whether it should be split
        if found_room is None:
            msg = 'No Room2D with the identifier "{}" was found in the ' \
                'Building.'.format(room_id)
            raise ValueError(msg)
        if len(split_heights) == 0:
            return []  # no splitting to be done

        # split the room across the stories
        new_rooms = []
        for i, (split_hgt, add_story) in enumerate(zip(split_heights, split_stories)):
            new_room = found_room.duplicate()
            new_room.identifier = '{}_split{}'.format(new_room.identifier, i)
            shift_dist = split_hgt - found_room.floor_height
            move_vec = Vector3D(0, 0, shift_dist)
            new_room.move(move_vec)  # move the room to the correct floor height
            try:
                new_ceil_hgt = split_heights[i + 1]
                new_room.is_top_exposed = False
                new_room.has_ceiling = False
            except IndexError:  # last story of the split list
                new_ceil_hgt = found_room.ceiling_height
            new_room.floor_to_ceiling_height = new_ceil_hgt - new_room.floor_height
            new_room.is_ground_contact = False
            new_room.has_floor = False
            new_w_par = []  # shift all of the window parameters for the room
            for wp, seg in zip(found_room.window_parameters, found_room.floor_segments):
                if isinstance(wp, _AsymmetricBase):
                    wp = wp.shift_vertically(-shift_dist)
                    wp.adjust_for_segment(seg, new_ceil_hgt, tolerance)
                new_w_par.append(wp)
            new_room.window_parameters = new_w_par
            add_story.add_room_2d(new_room)
            new_rooms.append(new_room)

        # change the height of the original Room2D so that it doesn't overlap new rooms
        found_room.floor_to_ceiling_height = split_heights[0] - found_room.floor_height
        found_room.is_top_exposed = False
        found_room.has_ceiling = False
        new_w_par = []  # shift all of the window parameters for the room
        for wp, seg in zip(found_room.window_parameters, found_room.floor_segments):
            if isinstance(wp, _AsymmetricBase):
                wp.adjust_for_segment(seg, found_room.floor_to_ceiling_height, tolerance)
            new_w_par.append(wp)
        found_room.window_parameters = new_w_par

        # move any roofs if need be
        if found_room.parent.roof is not None:
            kept_roofs, moved_roofs = [], []
            roof = found_room.parent.roof
            for r_geo, r_poly in zip(roof.geometry, roof.boundary_geometry_2d):
                room_poly = found_room.floor_geometry.boundary_polygon2d
                if room_poly.polygon_relationship(r_poly, tolerance) >= 0:
                    moved_roofs.append(r_geo)
                else:
                    kept_roofs.append(r_geo)
            if len(moved_roofs) != 0:
                if len(kept_roofs) != 0:
                    found_room.parent.roof = RoofSpecification(kept_roofs)
                else:
                    found_room.parent.roof = None
                if new_rooms[-1].parent.roof is None:
                    new_rooms[-1].parent.roof = RoofSpecification(moved_roofs)
                else:
                    new_geo = new_rooms[-1].parent.roof.geometry + tuple(moved_roofs)
                    new_rooms[-1].parent.roof = RoofSpecification(new_geo)

        return new_rooms

    def separate_room_2d_plenums(
            self, room_ids, target_floor_to_ceiling, floor_plenum=False,
            tolerance=0.01):
        """Separate a part of Room2D into ceiling (or floor) plenums.

        This method assumes that the Room2Ds' floor-to-ceiling-height is actually
        set to the floor-to-floor height. So this method reduces the floor-to-ceiling
        height of these rooms to what it is actually supposed to be and then
        adds a new ceiling (or floor) plenum Story to the Building with the
        plenums modeled as explicit Room2Ds. If an existing plenum Story of the
        correct type is found above (or below) the Room2D for which a plenum
        was split off, the new Room2D will get added to that existing Story
        instead of a new Story being created.

        Args:
            room_ids: A list of identifiers of Room2Ds within this Building, which
                will be split vertically to create a new plenum above (or below)
                the rooms.
            target_floor_to_ceiling: A number in model units for the desired
                floor-to-ceiling height of the final Room2Ds (assuming that this
                Room2D's current floor-to-ceiling height is actually the
                floor-to-floor height). If the current Room2D's floor-to-ceiling
                height is less than the input value, the floor-to-ceiling height
                will be reduced and a new ceiling or floor plenum created. This
                can also be a list of target_floor_to_ceiling heights that align
                with the input room_ids.
            floor_plenum: A boolean to note whether the plenum to be separated is
                a floor plenum for this current Room2D (in which case it is
                subtracted from the bottom) or it is a ceiling plenum (in which
                case it is subtracted from the top). (Default: False).
            tolerance: The maximum difference between point values for them to be
                considered distinct from one another. (Default: 0.01; suitable
                for objects in Meters).

        Returns:
            A list of all the new Room2Ds created by running the method. This
            can be used to post-process the rooms for attributes like
            floor/ceiling adjacency.
        """
        # handle the case of multiple target_floor_to_ceiling values
        t_ftc_i = 0
        if isinstance(target_floor_to_ceiling, (float, int)):
            target_floor_to_ceiling = [target_floor_to_ceiling] * len(room_ids)
        # set up variables to help manage the plenums
        room_ids, plenum_rm_ids = set(room_ids), set()
        new_rooms, new_stories, new_story_i = [], [], []
        resolve_roofs = False
        st_type = 'FloorPlenum' if floor_plenum else 'CeilingPlenum'

        # loop through the Room2Ds and split the plenum if they're selected
        for i, story in enumerate(self._unique_stories):
            if story.is_plenum:
                continue  # don't create plenums of plenums
            for rm in story.room_2ds:
                if rm.identifier in room_ids:
                    plenum_room = rm.separate_plenum(
                        target_floor_to_ceiling[t_ftc_i], floor_plenum, tolerance)
                    t_ftc_i += 1
                    if plenum_room is not None:
                        # add the room to the list of new rooms
                        new_rooms.append(plenum_room)
                        plenum_rm_ids.add(plenum_room.identifier)
                        # figure out on which Story the plenum belongs
                        pln_story = None
                        pln_st_i = i - 1 if floor_plenum else i + 1
                        if pln_st_i == -1 or pln_st_i == len(self._unique_stories) \
                                or not self._unique_stories[pln_st_i].type == st_type:
                            if pln_st_i in new_story_i:
                                for st, sti in zip(new_stories, new_story_i):
                                    if pln_st_i == sti:
                                        pln_story = st
                        else:  # there is already a relevant plenum story
                            pln_story = self._unique_stories[pln_st_i]
                        if pln_story is None:  # we must create a new story
                            pln_id = '{}_{}'.format(story.identifier, st_type)
                            pln_story = Story(pln_id, [plenum_room], type=st_type)
                            pln_story.floor_to_floor_height = story.floor_to_floor_height
                            pln_story.floor_height = story.floor_height
                            pln_story.multiplier = story.multiplier
                            if story._display_name is not None:
                                pln_story.display_name = \
                                    '{} Plenum'.format(story.display_name)
                            if story.roof is not None:
                                pln_story.roof = story.roof.duplicate()
                                resolve_roofs = True
                            new_stories.append(pln_story)
                            new_story_i.append(pln_st_i)
                        else:
                            pln_story.add_room_2d(plenum_room)

        # set up any adjacencies across the plenums
        try:  # get the boundary condition to be used for adiabatic cases
            ad_bc = bcs.adiabatic
        except AttributeError:
            ad_bc = bcs.outdoors  # honeybee_energy is not loaded; no adiabatic BC
        for new_room in new_rooms:
            new_bcs = []
            for i, bc in enumerate(new_room.boundary_conditions):
                if isinstance(bc, Surface):
                    if bc.boundary_condition_objects[-1] in plenum_rm_ids:
                        clean_bc = bc
                    else:
                        clean_bc = ad_bc
                        if not isinstance(ad_bc, Outdoors):
                            new_room._window_parameters[i] = None
                    new_bcs.append(clean_bc)
                else:
                    new_bcs.append(bc)
            new_room.boundary_conditions = new_bcs

        # remove adjacencies if plenum floor heights differ too much to be adjacent
        for story in new_stories:
            if story.check_room2d_floor_heights_valid(raise_exception=False) != '':
                min_ciel = min(rm.floor_to_ceiling_height for rm in story.room_2ds)
                room_groups, _ = Room2D.group_by_floor_height(story.room_2ds, min_ciel)
                for rm_group in room_groups:
                    Room2D.patch_missing_adjacencies(rm_group)

        # insert any newly-created stories into the Building
        for n_st in new_stories:
            n_st._parent = self
        new_stories = [st for _, st in sorted(zip(new_story_i, new_stories),
                                              key=lambda pair: pair[0])]
        new_story_i.sort()
        new_stories.reverse()  # reverse so that we insert from top to bottom
        new_story_i.reverse()  # reverse so that we insert from top to bottom
        all_stories = list(self._unique_stories)
        for st, sti in zip(new_stories, new_story_i):
            in_i = sti + 1 if floor_plenum else sti
            all_stories.insert(in_i, st)
        self._unique_stories = tuple(all_stories)

        # resolve the roofs if they were copied
        if resolve_roofs:
            self.remove_duplicate_roofs(tolerance)

        # set the extension properties of the plenums
        new_room_ids = [rm.identifier for rm in new_rooms]
        self.properties.make_plenums(new_room_ids)

        return new_rooms

    def convert_plenum_depths_to_room_2ds(self, tolerance=0.01):
        """Convert all of the Room2D ceiling/floor plenum depths to explicit Room2Ds.

        This method is used under the hood of the translation from Dragonfly to
        Honeybee in order to convert Room2D ceiling_plenum_depth and
        floor_plenum_depth properties into explicit 3D plenum Rooms.

        However, it may be useful to call it explicitly (outside of the translation)
        in order to edit the plenum Room2Ds. For example, multiple plenum Room2Ds
        above several rooms can be joined together into a single continuous
        plenum if this is how the plenum exists on the real building.

        Args:
            tolerance: The maximum difference between point values for them to be
                considered distinct from one another. (Default: 0.01; suitable
                for objects in Meters).

        Returns:
            A tuple with two elements.

            -   ceil_plenums: A list of Room2Ds for newly-created ceiling plenums
                from the ceiling_plenum_depths.

            -   floor_plenums: A list of Room2Ds for newly-created floor plenums
                from the floor_plenum_depths.
        """
        # gather all of the Room2D IDs with ceilings and floors to be split
        ceiling_rm_ids, ceiling_targets = [], []
        floor_rm_ids, floor_targets = [], []
        for room in self.unique_room_2ds:
            ftc = room.floor_to_ceiling_height
            cpd = room.ceiling_plenum_depth
            fpd = room.floor_plenum_depth
            if cpd == 0 and fpd == 0:
                continue  # no plenums to be generated
            elif cpd + fpd >= ftc - tolerance:
                continue  # invalid plenum depths that are too high
            elif not room.has_ceiling and cpd != 0:
                continue  # invalid plenum assigned to air boundary ceiling
            elif not room.has_floor and fpd != 0:
                continue  # invalid plenum assigned to air boundary floor
            if cpd != 0:
                ftc = ftc - cpd
                ceiling_rm_ids.append(room.identifier)
                ceiling_targets.append(ftc)
                room.ceiling_plenum_depth = 0  # set to zero now that plenum is explicit
            if fpd != 0:
                ftc = ftc - fpd
                floor_rm_ids.append(room.identifier)
                floor_targets.append(ftc)
                room.floor_plenum_depth = 0  # set to zero now that plenum is explicit
        # create the ceiling plenums
        ceil_plenums, floor_plenums = [], []
        if len(ceiling_rm_ids) != 0:
            ceil_plenums = self.separate_room_2d_plenums(
                ceiling_rm_ids, ceiling_targets, False, tolerance)
        if len(floor_rm_ids) != 0:
            floor_plenums = self.separate_room_2d_plenums(
                floor_rm_ids, floor_targets, True, tolerance)
        return ceil_plenums, floor_plenums

    def set_outdoor_window_parameters(self, window_parameter):
        """Set all of the outdoor walls to have the same window parameters."""
        for story in self._unique_stories:
            story.set_outdoor_window_parameters(window_parameter)

    def set_outdoor_shading_parameters(self, shading_parameter):
        """Set all of the outdoor walls to have the same shading parameters."""
        for story in self._unique_stories:
            story.set_outdoor_shading_parameters(shading_parameter)

    def to_rectangular_windows(self):
        """Convert all of the windows of the Story to the RectangularWindows format."""
        for story in self._unique_stories:
            story.to_rectangular_windows()

    def move(self, moving_vec):
        """Move this Building along a vector.

        Args:
            moving_vec: A ladybug_geometry Vector3D with the direction and distance
                to move the object.
        """
        for story in self._unique_stories:
            story.move(moving_vec)
        for room in self._room_3ds:
            room.move(moving_vec)
        self.properties.move(moving_vec)

    def rotate_xy(self, angle, origin):
        """Rotate this Building counterclockwise in the XY plane by a certain angle.

        Args:
            angle: An angle in degrees.
            origin: A ladybug_geometry Point3D for the origin around which the
                object will be rotated.
        """
        for story in self._unique_stories:
            story.rotate_xy(angle, origin)
        for room in self._room_3ds:
            room.rotate_xy(angle, origin)
        self.properties.rotate_xy(angle, origin)

    def reflect(self, plane):
        """Reflect this Building across a plane.

        Args:
            plane: A ladybug_geometry Plane across which the object will be reflected.
        """
        for story in self._unique_stories:
            story.reflect(plane)
        for room in self._room_3ds:
            room.reflect(plane)
        self.properties.reflect(plane)

    def scale(self, factor, origin=None):
        """Scale this Building by a factor from an origin point.

        Args:
            factor: A number representing how much the object should be scaled.
            origin: A ladybug_geometry Point3D representing the origin from which
                to scale. If None, it will be scaled from the World origin (0, 0, 0).
        """
        for story in self._unique_stories:
            story.scale(factor, origin)
        for room in self._room_3ds:
            room.scale(factor, origin)
        self.properties.scale(factor, origin)

    def has_floors_ceilings(self, use_multiplier=True):
        """Get a list of tuples for each Room noting whether it has a floor/ceiling.

        Args:
            use_multiplier: Boolean to note whether the returned list should be
                assume that the Rooms use multipliers or not. The list will typically
                be longer when use_multiplier is False. (Default: True).
        """
        has_flr_ceil = []
        for story in self._unique_stories:
            story_list = []
            for room in story.room_2ds:
                story_list.append((room.has_floor, room.has_ceiling))
            if use_multiplier:
                has_flr_ceil.extend(story_list)
            else:
                for _ in range(story.multiplier):
                    has_flr_ceil.extend(story_list)
        return has_flr_ceil

    def check_collisions_between_stories(
            self, tolerance=0.01, raise_exception=True, detailed=False):
        """Check that Room2Ds of each Story do not collide with others in the Building.

        Args:
            tolerance: The minimum distance that two Room2Ds geometries can collide
                with one another and still be considered valid. (Default: 0.01,
                suitable for objects in meters).
            raise_exception: Boolean to note whether a ValueError should be raised
                if colliding geometries are found. (Default: True).
            detailed: Boolean for whether the returned object is a detailed list of
                dicts with error info or a string with a message. (Default: False).

        Returns:
            A string with the message or a list with a dictionary if detailed is True.
        """
        detailed = False if raise_exception else detailed
        # loop through the stories and test for collisions
        msgs = []
        if len(self._unique_stories) > 1:
            stories = self._unique_stories
            for i, story1 in enumerate(stories):
                fh1 = story1.min_room_2d_floor_height
                ch1 = story1.max_room_2d_ceiling_height
                try:
                    for story2 in stories[i + 1:]:
                        fh2 = story2.min_room_2d_floor_height
                        ch2 = story2.max_room_2d_ceiling_height
                        v_overlap = 0
                        if fh1 < fh2 and ch1 - tolerance > fh2:
                            v_overlap = ch1 - fh2
                        elif fh2 < fh1 and ch2 - tolerance > fh1:
                            v_overlap = ch2 - fh1
                        if v_overlap != 0:
                            col_msg = story1.check_collision_with_story(
                                story2, tolerance, False, detailed)
                            if col_msg:
                                if detailed:
                                    msgs.extend(col_msg)
                                else:
                                    msgs.append(col_msg)
                except IndexError:
                    pass  # we have reached the end of the list of stories
        # process the output messages if issues were found
        if detailed:
            return msgs
        if len(msgs) != 0:
            msg = 'Building "{}" has stories with rooms that collide with one another' \
                ':\n {}'.format(self.full_id, '\n '.join(msgs))
            if raise_exception:
                raise ValueError(msg)
            return msg
        return ''

    def to_honeybee(self, use_multiplier=True, exclude_plenums=False, tolerance=0.01,
                    enforce_adj=True, enforce_solid=True):
        """Convert Dragonfly Building to a Honeybee Model.

        Args:
            use_multiplier: If True, the multipliers on this Building's Stories will be
                passed along to the generated Honeybee Room objects, indicating the
                simulation will be run once for each unique room and then results
                will be multiplied. If False, full geometry objects will be written
                for each and every floor in the building that are represented through
                multipliers and all resulting multipliers will be 1. (Default: True).
            exclude_plenums: Boolean to indicate whether ceiling/floor plenum depths
                assigned to Room2Ds should be ignored during translation. This
                results in each Room2D translating to a single Honeybee Room at
                the full floor_to_ceiling_height instead of a base Room with (a)
                plenum Room(s). (Default: False).
            tolerance: The minimum distance in z values of floor_height and
                floor_to_ceiling_height at which adjacent Faces will be split.
                Default: 0.01, suitable for objects in meters.
            enforce_adj: Boolean to note whether an exception should be raised if
                an adjacency between two Room2Ds is invalid (True) or if the invalid
                Surface boundary condition should be replaced with an Outdoor
                boundary condition (False). If False, any Walls containing
                WindowParameters and an illegal boundary condition will also
                be replaced with an Outdoor boundary condition. (Default: True).
            enforce_solid: Boolean to note whether rooms should be translated
                as solid extrusions whenever translating them with custom
                roof geometry produces a non-solid result (True) or the non-solid
                room geometry should be allowed to remain in the result (False).
                The latter is useful for understanding why a particular roof
                geometry has produced a non-solid result. (Default: True).

        Returns:
            A honeybee Model that represent the Building.
        """
        # separate the plenums unless they are excluded
        ceil_plenums, floor_plenums = [], []
        if not exclude_plenums and self.has_room_2d_plenums:
            self = self.duplicate()  # avoid mutating this Building instance
            ceil_plenums, floor_plenums = \
                self.convert_plenum_depths_to_room_2ds(tolerance)

        # compute the story heights once so they're not constantly recomputed
        reset_roofs = False
        if self._roofs is None:
            self._roofs = self._compute_roof_heights()
            reset_roofs = True

        # generate all of the Honeybee Rooms
        hb_rooms = []
        if use_multiplier:
            for story in self._unique_stories:
                hb_rooms.extend(
                    story.to_honeybee(True, tolerance=tolerance,
                                      enforce_adj=enforce_adj,
                                      enforce_solid=enforce_solid)
                )
        else:
            for story in self.all_stories():
                hb_rooms.extend(
                    story.to_honeybee(False, tolerance=tolerance,
                                      enforce_adj=enforce_adj,
                                      enforce_solid=enforce_solid)
                )
        for room in self.room_3ds:
            hb_rooms.append(room)
        hb_mod = Model(self.identifier, hb_rooms)
        hb_mod._display_name = self._display_name
        hb_mod._user_data = self._user_data

        # set adjacency between plenums and base rooms
        if len(ceil_plenums) != 0 or len(floor_plenums) != 0:
            # build up a map between rooms and identifiers
            room_map = {rm.identifier: rm for rm in hb_rooms}
            # use the map to set adjacencies for ceiling plenums
            if len(ceil_plenums) != 0:
                adj_ceil_pairs = []
                for ceil_pln in ceil_plenums:
                    pln_id = ceil_pln.identifier
                    base_id = pln_id[:-15]
                    adj_ceil_pairs.append((base_id, pln_id))
                    if not use_multiplier:
                        for i in range(ceil_pln.parent.multiplier - 1):
                            pln_id_i = 'Flr{}_{}'.format(i + 1, pln_id)
                            base_id_i = 'Flr{}_{}'.format(i + 1, base_id)
                            adj_ceil_pairs.append((base_id_i, pln_id_i))
                rm_faces, pln_faces = [], []
                for base_id, pln_id in adj_ceil_pairs:
                    room_face, plenum_face = room_map[base_id][-1], room_map[pln_id][0]
                    room_face.set_adjacency(plenum_face)
                    rm_faces.append(room_face)
                    pln_faces.append(plenum_face)
                self.properties.apply_ceiling_plenum_face_properties(rm_faces, pln_faces)
            # use the map to set adjacencies for floor plenums
            if len(floor_plenums) != 0:
                adj_floor_pairs = []
                for floor_pln in floor_plenums:
                    pln_id = floor_pln.identifier
                    base_id = pln_id[:-13]
                    adj_floor_pairs.append((base_id, pln_id))
                    if not use_multiplier:
                        for i in range(floor_pln.parent.multiplier - 1):
                            pln_id_i = 'Flr{}_{}'.format(i + 1, pln_id)
                            base_id_i = 'Flr{}_{}'.format(i + 1, base_id)
                            adj_floor_pairs.append((base_id_i, pln_id_i))
                rm_faces, pln_faces = [], []
                for base_id, pln_id in adj_floor_pairs:
                    room_face, plenum_face = room_map[base_id][0], room_map[pln_id][-1]
                    room_face.set_adjacency(plenum_face)
                    rm_faces.append(room_face)
                    pln_faces.append(plenum_face)
                self.properties.apply_floor_plenum_face_properties(rm_faces, pln_faces)

        # put back the old roofs if they were not set originally
        if reset_roofs:
            self._roofs = None
        return hb_mod

    def to_dict(self, abridged=False, included_prop=None):
        """Return Building as a dictionary.

        Args:
            abridged: Boolean to note whether the extension properties of the
                object (ie. construction sets) should be included in detail
                (False) or just referenced by identifier (True). Default: False.
            included_prop: List of properties to filter keys that must be included in
                output dictionary. For example ['energy'] will include 'energy' key if
                available in properties to_dict. By default all the keys will be
                included. To exclude all the keys from extensions use an empty list.
        """
        base = {'type': 'Building'}
        base['identifier'] = self.identifier
        base['display_name'] = self.display_name
        if len(self._unique_stories) != 0:
            base['unique_stories'] = [s.to_dict(abridged, included_prop)
                                      for s in self._unique_stories]
        if len(self._room_3ds) != 0:
            base['room_3ds'] = [r.to_dict(abridged, included_prop)
                                for r in self._room_3ds]
        base['properties'] = self.properties.to_dict(abridged, included_prop)
        if self.user_data is not None:
            base['user_data'] = self.user_data
        if self._roofs is not None:  # secret key used for filtered dictionaries
            rf_dicts = []
            for st_id, _, roof in self._roofs:
                rf_dicts.append((st_id, roof.to_dict()))
            base['_roofs'] = rf_dicts
        return base

    @property
    def to(self):
        """Building writer object.

        Use this method to access Writer class to write the building in other formats.
        """
        return writer

    @staticmethod
    def process_alleys(buildings, distance=1.0, adiabatic=False, tolerance=0.01):
        """Remove windows from any walls that within a distance of other buildings.

        This method can also optionally set the boundary conditions of these walls to
        adiabatic. This is helpful when attempting to account for alleys or parti walls
        that may exist between buildings of a denser urban district.

        Note that this staticmethod will edit the buildings in place so it may
        be appropriate to duplicate the Buildings before running this method.

        Args:
            buildings: Dragonfly Building objects which will have their windows removed
                if their walls lie within the distance of another building.
            distance: A number for the maximum distance of an alleyway in model
                units. If a wall is closer to another Building than this distance,
                the windows will be removed. (Default: 1.0; suitable for objects
                in meters).
            adiabatic: A boolean to note whether the walls that have their windows
                removed should also receive an Adiabatic boundary condition.
                This is useful when the alleyways are more like parti walls than
                distinct pathways that someone could traverse.
        """
        # get the adiabatic boundary condition in case we need it
        try:
            ad_bc = bcs.adiabatic
        except AttributeError:  # honeybee_energy is not loaded
            ad_bc = bcs.outdoors if not adiabatic else bcs.ground

        # get the footprints, heights and bounding points of all of the buildings
        story_heights, story_polys = [], []
        for bldg in buildings:
            bldg_polys, bldg_s_hgts = [], []
            for story in bldg.unique_stories:
                flr_hgt = story.floor_height
                bldg_s_hgts.append((flr_hgt, flr_hgt + story.floor_to_floor_height))
                story_foot = story.footprint(tolerance)
                st_poly = [Polygon2D((Point2D(p.x, p.y) for p in face.vertices))
                           for face in story_foot]
                bldg_polys.append(st_poly)
            story_heights.append(bldg_s_hgts)
            story_polys.append(bldg_polys)
        bldg_heights = [b.height for b in buildings]
        bldg_pts = []
        for bldg in buildings:
            b_min, b_max = bldg.min, bldg.max
            center = Point2D((b_min.x + b_max.x) / 2, (b_min.y + b_max.y) / 2)
            bldg_pts.append((b_min, center, b_max))

        # loop through the buildings and set the properties of the relevant walls
        for i, bldg in enumerate(buildings):
            # first determine the relevant buildings and building heights
            rel_st_polys, rel_st_heights, rel_b_heights = [], [], []
            other_indices = list(range(i)) + list(range(i + 1, len(buildings)))
            for j in other_indices:
                if Building._bound_rect_in_dist(bldg_pts[i], bldg_pts[j], distance):
                    rel_st_polys.append(story_polys[j])
                    rel_st_heights.append(story_heights[j])
                    rel_b_heights.append(bldg_heights[j])

            # then, loop through the story Room2Ds and set properties of relevant walls
            for story in bldg.unique_stories:
                st_hgt, st_f2f = story.floor_height, story.floor_to_floor_height
                st_c_hgt = st_hgt + st_f2f
                for rm in story.room_2ds:
                    zip_r_objs = zip(rm.boundary_conditions, rm.floor_segments_2d,
                                     rm.segment_normals)
                    new_bcs = list(rm.boundary_conditions)
                    new_win_pars = list(rm.window_parameters)
                    for k, (bc, seg, normal) in enumerate(zip_r_objs):
                        if not isinstance(bc, Outdoors):  # nothing to change
                            continue
                        seg_mid = seg.midpoint.move(normal * -tolerance)
                        seg_ray = LineSegment2D.from_sdl(seg_mid, normal, distance)
                        zip_b_objs = zip(rel_b_heights, rel_st_polys, rel_st_heights)
                        for bh, rel_poly, rel_hgt in zip_b_objs:
                            if st_hgt >= bh - tolerance:
                                continue  # story above other bldg; we can ignore it
                            for o_story, o_h in zip(rel_poly, rel_hgt):
                                overlap = min((o_h[1], st_c_hgt)) - max((o_h[0], st_hgt))
                                if overlap >= st_f2f * 0.33:  # more than 1/3 overlap
                                    for o_poly in o_story:
                                        if len(o_poly.intersect_line_ray(seg_ray)) > 0:
                                            # we have found an alleyway!
                                            new_win_pars[k] = None
                                            if adiabatic:
                                                new_bcs[k] = ad_bc
                                            break
                    # assign the new window parameters and boundary conditions
                    rm.window_parameters = new_win_pars
                    rm.boundary_conditions = new_bcs

    @staticmethod
    def district_to_honeybee(
            buildings, use_multiplier=True, exclude_plenums=False, tolerance=0.01,
            enforce_adj=True, enforce_solid=True):
        """Convert an array of Building objects into a single district honeybee Model.

        Args:
            buildings: An array of Building objects to be converted into a
                honeybee Model.
            use_multiplier: If True, the multipliers on this Building's Stories will be
                passed along to the generated Honeybee Room objects, indicating the
                simulation will be run once for each unique room and then results
                will be multiplied. If False, full geometry objects will be written
                for each and every floor in the building that are represented through
                multipliers and all resulting multipliers will be 1. (Default: True).
            exclude_plenums: Boolean to indicate whether ceiling/floor plenum depths
                assigned to Room2Ds should be ignored during translation. This
                results in each Room2D translating to a single Honeybee Room at
                the full floor_to_ceiling_height instead of a base Room with (a)
                plenum Room(s). (Default: False).
            tolerance: The minimum distance in z values of floor_height and
                floor_to_ceiling_height at which adjacent Faces will be split.
                Default: 0.01, suitable for objects in meters.
            enforce_adj: Boolean to note whether an exception should be raised if
                an adjacency between two Room2Ds is invalid (True) or if the invalid
                Surface boundary condition should be replaced with an Outdoor
                boundary condition (False). If False, any Walls containing
                WindowParameters and an illegal boundary condition will also
                be replaced with an Outdoor boundary condition. (Default: True).
            enforce_solid: Boolean to note whether rooms should be translated
                as solid extrusions whenever translating them with custom
                roof geometry produces a non-solid result (True) or the non-solid
                room geometry should be allowed to remain in the result (False).
                The latter is useful for understanding why a particular roof
                geometry has produced a non-solid result. (Default: True).

        Returns:
            A honeybee Model that represent the district.
        """
        # create a base model to which everything will be added
        base_model = buildings[0].to_honeybee(
            use_multiplier, exclude_plenums=exclude_plenums, tolerance=tolerance,
            enforce_adj=enforce_adj, enforce_solid=enforce_solid)
        # loop through each Building, create a model, and add it to the base one
        for bldg in buildings[1:]:
            base_model.add_model(
                bldg.to_honeybee(use_multiplier, exclude_plenums=exclude_plenums,
                                 tolerance=tolerance, enforce_adj=enforce_adj,
                                 enforce_solid=enforce_solid)
            )
        return base_model

    @staticmethod
    def buildings_to_honeybee(
            buildings, context_shades=None, shade_distance=None,
            use_multiplier=True, exclude_plenums=False, cap=False, tolerance=0.01,
            enforce_adj=True, enforce_solid=True):
        """Convert an array of Buildings into several honeybee Models with self-shading.

        Each input Building will be exported into its own Model. For each Model,
        the other input Buildings will appear as context shade geometry. Thus,
        each Model is its own simulate-able unit accounting for the total
        self-shading of the input Buildings.

        Args:
            buildings: An array of Building objects to be converted into honeybee
                Models that account for their own shading of one another.
            context_shades: An optional array of ContextShade objects that will be
                added to the honeybee Models if their bounding box overlaps with a
                given building within the shade_distance.
            shade_distance: An optional number to note the distance beyond which other
                objects' shade should not be exported into a given Model. This is
                helpful for reducing the simulation run time of each Model when other
                connected buildings are too far away to have a meaningful impact on
                the results. If None, all other buildings will be included as context
                shade in each and every Model. Set to 0 to exclude all neighboring
                buildings from the resulting models. Default: None.
            use_multiplier: If True, the multipliers on this Building's Stories will be
                passed along to the generated Honeybee Room objects, indicating the
                simulation will be run once for each unique room and then results
                will be multiplied. If False, full geometry objects will be written
                for each and every floor in the building that are represented through
                multipliers and all room multipliers will be 1. (Default: True).
            exclude_plenums: Boolean to indicate whether ceiling/floor plenum depths
                assigned to Room2Ds should be ignored during translation. This
                results in each Room2D translating to a single Honeybee Room at
                the full floor_to_ceiling_height instead of a base Room with (a)
                plenum Room(s). (Default: False).
            cap: Boolean to note whether building shade representations should be capped
                with a top face. Usually, this is not necessary to account for
                blocked sun and is only needed when it's important to account for
                reflected sun off of roofs. (Default: False).
            tolerance: The minimum distance in z values of floor_height and
                floor_to_ceiling_height at which adjacent Faces will be split.
                Default: 0.01, suitable for objects in meters.
            enforce_adj: Boolean to note whether an exception should be raised if
                an adjacency between two Room2Ds is invalid (True) or if the invalid
                Surface boundary condition should be replaced with an Outdoor
                boundary condition (False). If False, any Walls containing
                WindowParameters and an illegal boundary condition will also
                be replaced with an Outdoor boundary condition. (Default: True).
            enforce_solid: Boolean to note whether rooms should be translated
                as solid extrusions whenever translating them with custom
                roof geometry produces a non-solid result (True) or the non-solid
                room geometry should be allowed to remain in the result (False).
                The latter is useful for understanding why a particular roof
                geometry has produced a non-solid result. (Default: True).

        Returns:
            A list of honeybee Models that represent the Building.
        """
        # create lists with all context representations of the buildings + shade
        bldg_shades, bldg_pts, con_shades, con_pts = Building._honeybee_shades(
            buildings, context_shades, shade_distance, cap, tolerance)
        # loop through each Building and create a model
        models = []  # list to be filled with Honeybee Models
        num_bldg = len(buildings)
        for i, bldg in enumerate(buildings):
            model = bldg.to_honeybee(
                use_multiplier, exclude_plenums=exclude_plenums, tolerance=tolerance,
                enforce_adj=enforce_adj, enforce_solid=enforce_solid)
            Building._add_context_to_honeybee(model, bldg_shades, bldg_pts, con_shades,
                                              con_pts, shade_distance, num_bldg, i)
            models.append(model)  # append to the final list of Models
        return models

    @staticmethod
    def stories_to_honeybee(
            buildings, context_shades=None, shade_distance=None,
            use_multiplier=True, exclude_plenums=False, cap=False, tolerance=0.01,
            enforce_adj=True, enforce_solid=True):
        """Convert an array of Buildings into one honeybee Model per story.

        Each Story of each input Building will be exported into its own Model. For each
        Honeybee Model, the other input Buildings will appear as context shade geometry
        as will all of the other stories of the same building. Thus, each Model
        is its own simulate-able unit accounting for the total self-shading of
        the input Buildings.

        Args:
            buildings: An array of Building objects to be converted into an array of
                honeybee Models with one story per model.
            context_shades: An optional array of ContextShade objects that will be
                added to the honeybee Models if their bounding box overlaps with a
                given building within the shade_distance.
            shade_distance: An optional number to note the distance beyond which other
                objects' shade should not be exported into a given Model. This is
                helpful for reducing the simulation run time of each Model when other
                connected buildings are too far away to have a meaningful impact on
                the results. If None, all other buildings will be included as context
                shade in each and every Model. Set to 0 to exclude all neighboring
                buildings from the resulting models. Default: None.
            use_multiplier: If True, the multipliers on this Building's Stories will be
                passed along to the generated Honeybee Room objects, indicating the
                simulation will be run once for each unique room and then results
                will be multiplied. If False, full geometry objects will be written
                for each and every floor in the building that are represented through
                multipliers and all room multipliers will be 1. (Default: True).
            exclude_plenums: Boolean to indicate whether ceiling/floor plenum depths
                assigned to Room2Ds should be ignored during translation. This
                results in each Room2D translating to a single Honeybee Room at
                the full floor_to_ceiling_height instead of a base Room with (a)
                plenum Room(s). (Default: False).
            cap: Boolean to note whether building shade representations should be capped
                with a top face. Usually, this is not necessary to account for
                blocked sun and is only needed when it's important to account for
                reflected sun off of roofs. (Default: False).
            tolerance: The minimum distance in z values of floor_height and
                floor_to_ceiling_height at which adjacent Faces will be split.
                Default: 0.01, suitable for objects in meters.
            enforce_adj: Boolean to note whether an exception should be raised if
                an adjacency between two Room2Ds is invalid (True) or if the invalid
                Surface boundary condition should be replaced with an Outdoor
                boundary condition (False). If False, any Walls containing
                WindowParameters and an illegal boundary condition will also
                be replaced with an Outdoor boundary condition. (Default: True).
            enforce_solid: Boolean to note whether rooms should be translated
                as solid extrusions whenever translating them with custom
                roof geometry produces a non-solid result (True) or the non-solid
                room geometry should be allowed to remain in the result (False).
                The latter is useful for understanding why a particular roof
                geometry has produced a non-solid result. (Default: True).

        Returns:
            A list of honeybee Models that represent the Stories.
        """
        # create lists with all context representations of the buildings + shade
        bldg_shades, bldg_pts, con_shades, con_pts = Building._honeybee_shades(
            buildings, context_shades, shade_distance, cap, tolerance)
        # loop through each Building and create a model
        models = []  # list to be filled with Honeybee Models
        num_bldg = len(buildings)
        for i, bldg in enumerate(buildings):
            dummy_model = Model(bldg.identifier)  # blank model to hold context shade
            Building._add_context_to_honeybee(
                dummy_model, bldg_shades, bldg_pts, con_shades, con_pts,
                shade_distance, num_bldg, i)
            bldg_con = list(dummy_model.orphaned_shades)
            if use_multiplier:
                for j, story in enumerate(bldg.unique_stories):
                    if not exclude_plenums and story.has_plenums:
                        plenum_bldg = Building(story.identifier, [story.duplicate()])
                        dummy_model = plenum_bldg.to_honeybee(
                            True, False, tolerance=tolerance,
                            enforce_adj=enforce_adj, enforce_solid=enforce_solid)
                        hb_rooms = dummy_model.rooms
                    else:
                        hb_rooms = story.to_honeybee(
                            True, tolerance=tolerance,
                            enforce_adj=enforce_adj, enforce_solid=enforce_solid)
                    if bldg.has_room_3ds:
                        hb_rooms.extend(bldg.room_3ds_by_story(story.display_name))
                    shds = bldg_con + bldg.shade_representation(j, cap, False, tolerance)
                    model = Model(story.identifier, hb_rooms, orphaned_shades=shds)
                    model.display_name = story.display_name
                    models.append(model)  # append to the final list of Models
            else:
                self_shds = [story.shade_representation(cap, tolerance)
                             for story in bldg.unique_stories if not story.is_plenum]
                full_shades = []
                for j, story in enumerate(bldg.unique_stories):
                    if not story.is_plenum:
                        for k in range(story.multiplier):
                            mult_shd = story.shade_representation_multiplier(
                                k, cap=cap, tolerance=tolerance)
                            mult_shd.extend([s for s_ar in self_shds[:j] for s in s_ar])
                            mult_shd.extend([s for s_ar in self_shds[j + 1:] for s in s_ar])
                            full_shades.append(mult_shd)
                for story, shades in zip(bldg.all_stories(), full_shades):
                    if not exclude_plenums and story.has_plenums:
                        plenum_bldg = Building(story.identifier, [story.duplicate()])
                        dummy_model = plenum_bldg.to_honeybee(
                            True, False, tolerance=tolerance,
                            enforce_adj=enforce_adj, enforce_solid=enforce_solid)
                        hb_rooms = dummy_model.rooms
                    else:
                        hb_rooms = story.to_honeybee(
                            True, tolerance=tolerance,
                            enforce_adj=enforce_adj, enforce_solid=enforce_solid)
                    if bldg.has_room_3ds:
                        hb_rooms.extend(bldg.room_3ds_by_story(story.display_name))
                    shds = bldg_con + shades
                    model = Model(story.identifier, hb_rooms, orphaned_shades=shds)
                    model.display_name = story.display_name
                    models.append(model)  # append to the final list of Models
            if bldg.has_room_3ds:  # organize them by story and add them
                accounted_for = bldg.room_2d_story_names
                r3_story_dict = bldg._story_dict_room_3d()
                shds = bldg_con + bldg.shade_representation(
                    None, cap, False, tolerance)
                for story_id, hb_rooms in r3_story_dict.items():
                    if story_id not in accounted_for:
                        model = Model(story_id, hb_rooms, orphaned_shades=shds)
                        models.append(model)  # append to the final list of Models
        return models

    def _compute_roof_heights(self):
        """Get a list with the center height of each RoofSpecification in the Building.

        This method is used internally during Honeybee serialization.
        """
        roof_specs = []
        for story in self._unique_stories:
            if story.roof is not None:
                rf_height = (story.roof.max_height + story.roof.min_height) / 2
                roof_specs.append((story.identifier, rf_height, story.roof))
            else:
                roof_specs.append((story.identifier, None, None))
        return roof_specs

    def _story_roofs(self, story):
        """Get a list of RoofSpecifications that are relevant for a given Story.

        The returned list will contain tuples where the first item is the center
        height of the roof and the second item is the RoofSpecification object.
        This is used under the hood to determine whether roofs of other Stories
        should influence a given Room2D.

        Args:
            story: A Story object within the Building.
        """
        # compute the center height of each roof specification
        bldg_roofs = self._compute_roof_heights() if self._roofs is None else self._roofs
        # filter out the roofs for the relevant story
        rel_roofs, story_found = [], False
        for st_id, hgt, roof in bldg_roofs:
            if story_found and roof is not None:
                rel_roofs.append((hgt, roof))
            if story.identifier == st_id:
                story_found = True
        return rel_roofs

    def _story_dict_room_3d(self):
        """Get a dictionary of 3D Honeybee Rooms organized by story."""
        r3_story_dict = {}
        for room in self._room_3ds:
            try:
                r3_story_dict[room.story].append(room)
            except KeyError:
                r3_story_dict[room.story] = [room]
        return r3_story_dict

    def _lowest_story_room_3ds(self):
        """Get a list of Honeybee Rooms for the lowest story of the Building.

        Note that this method should typically only be used when the Building is
        composed entirely of 3D Honeybee Rooms.
        """
        r3_story_dict = self._story_dict_room_3d()
        floor_hgts, floor_rooms = [], []
        for rooms in r3_story_dict.values():
            flr_hgt = sum(r.average_floor_height for r in rooms) / len(rooms)
            floor_hgts.append(flr_hgt)
            floor_rooms.append(rooms)
        sort_rooms = [rs for _, rs in sorted(zip(floor_hgts, floor_rooms),
                                             key=lambda pair: pair[0])]
        return sort_rooms[0]

    def _check_geometry_exists(self):
        if len(self._unique_stories) == 0 and len(self._room_3ds) == 0:
            raise ValueError('Building must have some geometry - at least one Story '
                             'or one Room under room_3ds.')

    @staticmethod
    def _is_room_flat_top_extrusion(hb_room, tolerance, angle_tolerance):
        """Test if a 3D Honeybee Room is a flat-topped extrusion.

        This will only be True if all Faces in the Room are vertical or horizontal
        within the angle tolerance.

        Args:
            hb_room: The 3D Honeybee Room to be tested.
            tolerance: The absolute tolerance with which the Room geometry will
                be evaluated.
            angle_tolerance: The angle tolerance at which the geometry will
                be evaluated in degrees.

        Returns:
            True if the 3D Room is a flat-topped extrusion. False if not.
        """
        # set up the parameters for evaluating vertical or horizontal
        vert_vec = Vector3D(0, 0, 1)
        min_v_ang = math.radians(angle_tolerance)
        max_v_ang = math.pi - min_v_ang
        min_h_ang = (math.pi / 2) - min_v_ang
        max_h_ang = (math.pi / 2) + min_v_ang

        # loop through the 3D Room faces and test them
        for face in hb_room._faces:
            try:  # first make sure that the geometry is not degenerate
                clean_geo = face.geometry.remove_colinear_vertices(tolerance)
                v_ang = clean_geo.normal.angle(vert_vec)
                if v_ang <= min_v_ang or v_ang >= max_v_ang:
                    continue
                elif min_h_ang <= v_ang <= max_h_ang:
                    continue
                return False
            except AssertionError:  # degenerate face to ignore
                pass
        return True

    @staticmethod
    def _is_room_sloped_extrusion(hb_room, tolerance, angle_tolerance):
        """Test if a 3D Honeybee Room is a sloped-roof extrusion.

        This will only be True if all Floor Faces of the Room are horizontal
        and all Wall Faces are vertical within the angle tolerance. Roof Faces
        are permitted to have any level of slope as long as they are pointing
        upwards. AirBoundary Faces must be vertical or horizontal.

        Args:
            hb_room: The 3D Honeybee Room to be tested.
            tolerance: The absolute tolerance with which the Room geometry will
                be evaluated.
            angle_tolerance: The angle tolerance at which the geometry will
                be evaluated in degrees.

        Returns:
            True if the 3D Room is a sloped-roof extrusion. False if not.
        """
        # set up the parameters for evaluating vertical or horizontal
        vert_vec = Vector3D(0, 0, 1)
        min_v_ang = math.radians(angle_tolerance)
        max_v_ang = math.pi - min_v_ang
        min_h_ang = (math.pi / 2) - min_v_ang
        max_h_ang = (math.pi / 2) + min_v_ang
        sloped_count = 0

        # loop through the 3D Room faces and test them
        for face in hb_room._faces:
            try:  # first make sure that the geometry is not degenerate
                clean_geo = face.geometry.remove_colinear_vertices(tolerance)
                v_ang = clean_geo.normal.angle(vert_vec)
                if isinstance(face.type, Wall):
                    if min_h_ang <= v_ang <= max_h_ang:
                        continue
                elif isinstance(face.type, Floor):
                    if v_ang <= min_v_ang or v_ang >= max_v_ang:
                        continue
                elif isinstance(face.type, RoofCeiling):
                    if v_ang <= min_v_ang or v_ang >= max_v_ang:
                        continue
                    elif v_ang <= min_h_ang:
                        sloped_count += 1
                        continue
                else:  # AirBoundary Faces must be vertical or horizontal
                    if v_ang <= min_v_ang or v_ang >= max_v_ang:
                        continue
                    elif min_h_ang <= v_ang <= max_h_ang:
                        continue
                return False
            except AssertionError:  # degenerate face to ignore
                pass

        # if there are too many roof faces (like a dome), just keep it 3D
        if sloped_count > 25:
            return False
        return True

    @staticmethod
    def _honeybee_shades(buildings, context_shades, shade_distance, cap, tolerance):
        """Get lists of Honeybee shades from Building and ContextShade objects."""
        bldg_shades, bldg_pts = [], []
        con_shades, con_pts = [], []
        if shade_distance is None or shade_distance > 0:
            for bldg in buildings:
                b_shades = bldg.shade_representation(
                    cap=cap, include_room3ds=True, tolerance=tolerance)
                bldg_shades.append(b_shades)
                b_min, b_max = bldg.min, bldg.max
                center = Point2D((b_min.x + b_max.x) / 2, (b_min.y + b_max.y) / 2)
                bldg_pts.append((b_min, center, b_max))
            if context_shades is not None:
                for con in context_shades:
                    con_shades.append(con.to_honeybee())
                    c_min, c_max = con.min, con.max
                    center = Point2D((c_min.x + c_max.x) / 2, (c_min.y + c_max.y) / 2)
                    con_pts.append((c_min, center, c_max))
        return bldg_shades, bldg_pts, con_shades, con_pts

    @staticmethod
    def _add_context_to_honeybee(model, bldg_shades, bldg_pts, con_shades, con_pts,
                                 shade_distance, num_bldg, i):
        """Add context shades to a Honeybee Model based on shade distance."""
        if shade_distance is None:  # add all other bldg shades to the model
            for j in xrange(i + 1, num_bldg):  # buildings before this one
                for shd in bldg_shades[j]:
                    model.add_shade(shd)
            for k in xrange(i):  # buildings after this one
                for shd in bldg_shades[k]:
                    model.add_shade(shd)
            for c_shade in con_shades:  # context shades
                for shd in c_shade:
                    if isinstance(shd, Shade):
                        model.add_shade(shd)
                    else:
                        model.add_shade_mesh(shd)
        elif shade_distance > 0:  # add only shade within the distance
            for j in xrange(i + 1, num_bldg):  # buildings before this one
                if Building._bound_rect_in_dist(bldg_pts[i], bldg_pts[j],
                                                shade_distance):
                    for shd in bldg_shades[j]:
                        model.add_shade(shd)
            for k in xrange(i):  # buildings after this one
                if Building._bound_rect_in_dist(bldg_pts[i], bldg_pts[k],
                                                shade_distance):
                    for shd in bldg_shades[k]:
                        model.add_shade(shd)
            for s in xrange(len(con_shades)):  # context shades
                if Building._bound_rect_in_dist(bldg_pts[i], con_pts[s],
                                                shade_distance):
                    for shd in con_shades[s]:
                        if isinstance(shd, Shade):
                            model.add_shade(shd)
                        else:
                            model.add_shade_mesh(shd)

    @staticmethod
    def _generate_room_2ds(face3d_array, flr_to_ceiling, perim_offset,
                           bldg_id, flr_count, tolerance):
        """Generate Room2D objects given geometry and information about their parent.

        Args:
            face3d_array: An array of Face3D objects to be turned into a Story's Room2Ds.
            flr_to_ceiling: The floor-to-ceiling height to use for all the Room2Ds.
            perim_offset: A perimeter offset to be used to subdivide Face3Ds
            bldg_id: Text for the identifier to which the rooms belong.
            flr_count: Integer for the which story the building belongs to.
            tolerance: Tolerance to be used in the creation of the Room2Ds.
        """
        # if there is a non-zero perimeter offset, separate core vs. perimeter zones
        if perim_offset != 0:
            assert perim_offset > 0, 'perimeter_offset cannot be less than than 0.'
            new_face3d_array = []
            for floor_face in face3d_array:
                try:
                    floor_face = floor_face.remove_colinear_vertices(tolerance)
                    perimeter, core = perimeter_core_subfaces(
                        floor_face, perim_offset, tolerance)
                    new_face3d_array.extend(perimeter)
                    new_face3d_array.extend(core)
                except Exception as e:  # the generation of the polyskel failed
                    print('Core/perimeter generation failed:\n{}'.format(e))
                    new_face3d_array.append(floor_face)  # just use existing floor
            face3d_array = new_face3d_array  # replace with offset core/perimeter

        # create the Room2D objects
        room_2ds = []
        for i, room_geo in enumerate(face3d_array):
            room = Room2D('{}_Floor{}_Room{}'.format(bldg_id, flr_count, i + 1),
                          room_geo, flr_to_ceiling, tolerance=tolerance)
            room_2ds.append(room)

        # solve for interior adjacency if there core/perimeter zoning was requested
        if perim_offset != 0:
            room_2ds = Room2D.intersect_adjacency(
                room_2ds, tolerance, preserve_wall_props=False)
            Room2D.solve_adjacency(room_2ds, tolerance)
        return room_2ds

    @staticmethod
    def _is_story_equivalent(face1, face2, tolerance):
        """Check whether area, XY centerpoint and XY first point match between Face3D.

        Args:
            face1: First Face3D to check.
            face2: Second Face3D to check.
            tolerance: The maximum difference between x, y, and z values at which
                point vertices are considered to be the same.

        Returns:
            True if face1 is geometrically equivalent to face 2 else False.
        """
        # check wether the center points match within tolerance.
        cent1 = face1.center
        cent2 = face2.center
        if abs(cent1.x - cent2.x) > tolerance or abs(cent1.y - cent2.y) > tolerance:
            return False

        # check wether the point at start matches within tolerance
        start1 = face1[0]
        start2 = face2[0]
        if abs(start1.x - start2.x) > tolerance or abs(start1.y - start2.y) > tolerance:
            return False

        # check whether areas match within tolerance
        area_tol = tolerance ** 2
        if abs(face1.area - face2.area) > area_tol:
            return False

        return True

    @staticmethod
    def _bound_rect_in_dist(bound_pts1, bound_pts2, distance):
        """Check if the bounding rectangles of two footprints overlap within a distance.

        Checking the overlap of the bounding rectangles is extremely quick given this
        method's use of the Separating Axis Theorem.

        Args:
            bound_pts1: An array of Point2Ds (min, center, max) for the first footprint.
            bound_pts2: An array of Point2Ds (min, center, max) for the second footprint.
            distance: Acceptable distance between the two bounding rectangles.
        """
        # Bounding rectangle check using the Separating Axis Theorem
        polygon1_width = bound_pts1[2].x - bound_pts1[0].x
        polygon2_width = bound_pts2[2].x - bound_pts2[0].x
        dist_btwn_x = abs(bound_pts1[1].x - bound_pts2[1].x)
        x_gap_btwn_rect = dist_btwn_x - (0.5 * polygon1_width) - (0.5 * polygon2_width)

        polygon1_height = bound_pts1[2].y - bound_pts1[0].y
        polygon2_height = bound_pts2[2].y - bound_pts2[0].y
        dist_btwn_y = abs(bound_pts1[1].y - bound_pts2[1].y)
        y_gap_btwn_rect = dist_btwn_y - (0.5 * polygon1_height) - (0.5 * polygon2_height)

        if x_gap_btwn_rect > distance or y_gap_btwn_rect > distance:
            return False  # no overlap
        return True  # overlap exists

    @staticmethod
    def _separated_ground_floor(base_story):
        """Get a separated ground floor from a base_story."""
        bottom = base_story.duplicate()  # generate a new bottom floor
        bottom.multiplier = 1
        bottom.add_prefix('Ground')
        return bottom

    @staticmethod
    def _separated_top_floor(base_story):
        """Get a separated top floor from a base_story."""
        top = base_story.duplicate()  # generate a new top floor
        move_vec = Vector3D(0, 0, top.floor_to_floor_height * (top.multiplier - 1))
        top.move(move_vec)
        top.multiplier = 1
        top.add_prefix('Top')
        return top

    def __copy__(self):
        new_b = Building(
            self.identifier, tuple(story.duplicate() for story in self._unique_stories),
            tuple(room.duplicate() for room in self._room_3ds))
        new_b._display_name = self._display_name
        new_b._user_data = None if self.user_data is None else self.user_data.copy()
        new_b._properties._duplicate_extension_attr(self._properties)
        return new_b

    def __len__(self):
        return len(self._unique_stories)

    def __getitem__(self, key):
        return self._unique_stories[key]

    def __iter__(self):
        return iter(self._unique_stories)

    def __repr__(self):
        return 'Building: %s' % self.display_name
