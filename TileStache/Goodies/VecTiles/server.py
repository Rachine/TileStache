''' Provider that returns PostGIS vector tiles in GeoJSON or MVT format.

VecTiles is intended for rendering, and returns tiles with contents simplified,
precision reduced and often clipped. The MVT format in particular is designed
for use in Mapnik with the VecTiles Datasource, which can read binary MVT tiles.

For a more general implementation, try the Vector provider:
    http://tilestache.org/doc/#vector-provider
'''
from math import pi
from urlparse import urljoin, urlparse
from urllib import urlopen
from os.path import exists

import json
from ... import getTile
from ...Core import KnownUnknown

try:
    from psycopg2.extras import RealDictCursor
    from psycopg2 import connect

except ImportError, err:
    # Still possible to build the documentation without psycopg2

    def connect(*args, **kwargs):
        raise err

from . import mvt, geojson, topojson, oscimap, mapbox
from ...Geography import SphericalMercator
from ModestMaps.Core import Point

tolerances = [6378137 * 2 * pi / (2 ** (zoom + 8)) for zoom in range(22)]

class Provider:
    ''' VecTiles provider for PostGIS data sources.
    
        Parameters:
        
          dbinfo:
            Required dictionary of Postgres connection parameters. Should
            include some combination of 'host', 'user', 'password', and 'database'.
        
          queries:
            Required list of Postgres queries, one for each zoom level. The
            last query in the list is repeated for higher zoom levels, and null
            queries indicate an empty response.
            
            Query must use "__geometry__" for a column name, and must be in
            spherical mercator (900913) projection. A query may include an
            "__id__" column, which will be used as a feature ID in GeoJSON
            instead of a dynamically-generated hash of the geometry. A query
            can additionally be a file name or URL, interpreted relative to
            the location of the TileStache config file.
            
            If the query contains the token "!bbox!", it will be replaced with
            a constant bounding box geomtry like this:
            "ST_SetSRID(ST_MakeBox2D(ST_MakePoint(x, y), ST_MakePoint(x, y)), <srid>)"
            
            This behavior is modeled on Mapnik's similar bbox token feature:
            https://github.com/mapnik/mapnik/wiki/PostGIS#bbox-token
          
          clip:
            Optional boolean flag determines whether geometries are clipped to
            tile boundaries or returned in full. Default true: clip geometries.
        
          srid:
            Optional numeric SRID used by PostGIS for spherical mercator.
            Default 900913.
        
          simplify:
            Optional floating point number of pixels to simplify all geometries.
            Useful for creating double resolution (retina) tiles set to 0.5, or
            set to 0.0 to prevent any simplification. Default 1.0.
        
          simplify_until:
            Optional integer specifying a zoom level where no more geometry
            simplification should occur. Default 16.
        
        Sample configuration, for a layer with no results at zooms 0-9, basic
        selection of lines with names and highway tags for zoom 10, a remote
        URL containing a query for zoom 11, and a local file for zooms 12+:
        
          "provider":
          {
            "class": "TileStache.Goodies.VecTiles:Provider",
            "kwargs":
            {
              "dbinfo":
              {
                "host": "localhost",
                "user": "gis",
                "password": "gis",
                "database": "gis"
              },
              "queries":
              [
                null, null, null, null, null,
                null, null, null, null, null,
                "SELECT way AS __geometry__, highway, name FROM planet_osm_line -- zoom 10+ ",
                "http://example.com/query-z11.pgsql",
                "query-z12-plus.pgsql"
              ]
            }
          }
    '''
    def __init__(self, layer, dbinfo, queries, clip=True, srid=900913, simplify=1.0, simplify_until=16):
        '''
        '''
        self.layer = layer
        
        keys = 'host', 'user', 'password', 'database', 'port', 'dbname'
        self.dbinfo = dict([(k, v) for (k, v) in dbinfo.items() if k in keys])

        self.clip = bool(clip)
        self.srid = int(srid)
        self.simplify = float(simplify)
        self.simplify_until = int(simplify_until)
        
        self.queries = []
        self.columns = {}
        
        for query in queries:
            if query is None:
                self.queries.append(None)
                continue
        
            #
            # might be a file or URL?
            #
            url = urljoin(layer.config.dirpath, query)
            scheme, h, path, p, q, f = urlparse(url)
            
            if scheme in ('file', '') and exists(path):
                query = open(path).read()
            
            elif scheme == 'http' and ' ' not in url:
                query = urlopen(url).read()
        
            self.queries.append(query)
        
    def renderTile(self, width, height, srs, coord):
        ''' Render a single tile, return a Response instance.
        '''
        try:
            query = self.queries[coord.zoom]
        except IndexError:
            query = self.queries[-1]

        ll = self.layer.projection.coordinateProj(coord.down())
        ur = self.layer.projection.coordinateProj(coord.right())
        bounds = ll.x, ll.y, ur.x, ur.y
        
        if not query:
            return EmptyResponse(bounds)
        
        if query not in self.columns:
            self.columns[query] = query_columns(self.dbinfo, self.srid, query, bounds)
        
        tolerance = self.simplify * tolerances[coord.zoom] if coord.zoom < self.simplify_until else None

        return Response(self.dbinfo, self.srid, query, self.columns[query], bounds, tolerance, coord.zoom, self.clip, coord, self.layer.name())

    def getTypeByExtension(self, extension):
        ''' Get mime-type and format by file extension, one of "mvt", "json" or "topojson".
        '''
        if extension.lower() == 'mvt':
            return 'application/octet-stream+mvt', 'MVT'
        
        elif extension.lower() == 'json':
            return 'application/json', 'JSON'
        
        elif extension.lower() == 'topojson':
            return 'application/json', 'TopoJSON'

        elif extension.lower() == 'vtm':
            return 'image/png', 'OpenScienceMap' # TODO: make this proper stream type, app only seems to work with png

        elif extension.lower() == 'mapbox':
            return 'image/png', 'Mapbox' 

        else:
            raise ValueError(extension + " is not a valid extension")

class MultiProvider:
    ''' VecTiles provider to gather PostGIS tiles into a single multi-response.
        
        Returns a MultiResponse object for GeoJSON or TopoJSON requests.
    
        names:
          List of names of vector-generating layers from elsewhere in config.
        
        Sample configuration, for a layer with combined data from water
        and land areas, both assumed to be vector-returning layers:
        
          "provider":
          {
            "class": "TileStache.Goodies.VecTiles:MultiProvider",
            "kwargs":
            {
              "names": ["water-areas", "land-areas"]
            }
          }
    '''
    def __init__(self, layer, names):
        self.layer = layer
        self.names = names
    
    def __call__(self, layer, names):
        self.layer = layer
        self.names = names

    def renderTile(self, width, height, srs, coord):
        ''' Render a single tile, return a Response instance.
        '''
        return MultiResponse(self.layer.config, self.names, coord)

    def getTypeByExtension(self, extension):
        ''' Get mime-type and format by file extension, "json" or "topojson" only.
        '''
        if extension.lower() == 'json':
            return 'application/json', 'JSON'
        
        elif extension.lower() == 'topojson':
            return 'application/json', 'TopoJSON'

        elif extension.lower() == 'vtm':
            return 'image/png', 'OpenScienceMap' # TODO: make this proper stream type, app only seems to work with png
        
        elif extension.lower() == 'mapbox':
            return 'image/png', 'Mapbox' 

        else:
            raise ValueError(extension + " is not a valid extension for responses with multiple layers")

class Connection:
    ''' Context manager for Postgres connections.
    
        See http://www.python.org/dev/peps/pep-0343/
        and http://effbot.org/zone/python-with-statement.htm
    '''
    def __init__(self, dbinfo):
        self.dbinfo = dbinfo
    
    def __enter__(self):
        self.db = connect(**self.dbinfo).cursor(cursor_factory=RealDictCursor)
        return self.db
    
    def __exit__(self, type, value, traceback):
        self.db.connection.close()

class Response:
    '''
    '''
    def __init__(self, dbinfo, srid, subquery, columns, bounds, tolerance, zoom, clip, coord, layer_name):
        ''' Create a new response object with Postgres connection info and a query.
        
            bounds argument is a 4-tuple with (xmin, ymin, xmax, ymax).
        '''
        self.dbinfo = dbinfo
        self.bounds = bounds
        self.zoom = zoom
        self.clip = clip
        self.coord= coord
        self.layer_name = layer_name
        
        geo_query = build_query(srid, subquery, columns, bounds, tolerance, True, clip)
        merc_query = build_query(srid, subquery, columns, bounds, tolerance, False, clip)
        oscimap_query = build_query(srid, subquery, columns, bounds, tolerance, False, clip, oscimap.padding * tolerances[coord.zoom], oscimap.extents)
        mapbox_query = build_query(srid, subquery, columns, bounds, tolerance, False, clip, mapbox.padding * tolerances[coord.zoom], mapbox.extents)
        self.query = dict(TopoJSON=geo_query, JSON=geo_query, MVT=merc_query, OpenScienceMap=oscimap_query, Mapbox=mapbox_query)

    def save(self, out, format):
        '''
        '''
        features = get_features(self.dbinfo, self.query[format])

        if format == 'MVT':
            mvt.encode(out, features)
        
        elif format == 'JSON':
            geojson.encode(out, features, self.zoom, self.clip)
        
        elif format == 'TopoJSON':
            ll = SphericalMercator().projLocation(Point(*self.bounds[0:2]))
            ur = SphericalMercator().projLocation(Point(*self.bounds[2:4]))
            topojson.encode(out, features, (ll.lon, ll.lat, ur.lon, ur.lat), self.clip)

        elif format == 'OpenScienceMap':
            oscimap.encode(out, features, self.coord, self.layer_name)

        elif format == 'Mapbox':
            mapbox.encode(out, features, self.coord, self.layer_name)

        else:
            raise ValueError(format + " is not supported")

class EmptyResponse:
    ''' Simple empty response renders valid MVT or GeoJSON with no features.
    '''
    def __init__(self, bounds):
        self.bounds = bounds
    
    def save(self, out, format):
        '''
        '''
        if format == 'MVT':
            mvt.encode(out, [])
        
        elif format == 'JSON':
            geojson.encode(out, [], 0, False)
        
        elif format == 'TopoJSON':
            ll = SphericalMercator().projLocation(Point(*self.bounds[0:2]))
            ur = SphericalMercator().projLocation(Point(*self.bounds[2:4]))
            topojson.encode(out, [], (ll.lon, ll.lat, ur.lon, ur.lat), False)

        elif format == 'OpenScienceMap':
            oscimap.encode(out, [], None)

        elif format == 'Mapbox':
            mapbox.encode(out, [], None)

        else:
            raise ValueError(format + " is not supported")

class MultiResponse:
    '''
    '''
    def __init__(self, config, names, coord):
        ''' Create a new response object with TileStache config and layer names.
        '''
        self.config = config
        self.names = names
        self.coord = coord
    def save(self, out, format):
        '''
        '''
        if format == 'TopoJSON':
            topojson.merge(out, self.names, self.get_tiles(format), self.config, self.coord)
        
        elif format == 'JSON':
            geojson.merge(out, self.names, self.get_tiles(format), self.config, self.coord)

        elif format == 'OpenScienceMap':
            feature_layers = []
            layers = [self.config.layers[name] for name in self.names]
            for layer in layers:
                width, height = layer.dim, layer.dim
                tile = layer.provider.renderTile(width, height, layer.projection.srs, self.coord)
                if isinstance(tile,EmptyResponse): continue
                feature_layers.append({'name': layer.name(), 'features': get_features(tile.dbinfo, tile.query["OpenScienceMap"])})
            oscimap.merge(out, feature_layers, self.coord)
        
        elif format == 'Mapbox':
            feature_layers = []
            layers = [self.config.layers[name] for name in self.names]
            for layer in layers:
                width, height = layer.dim, layer.dim
                tile = layer.provider.renderTile(width, height, layer.projection.srs, self.coord)
                if isinstance(tile,EmptyResponse): continue
                feature_layers.append({'name': layer.name(), 'features': get_features(tile.dbinfo, tile.query["Mapbox"])})
            mapbox.merge(out, feature_layers, self.coord)

        else:
            raise ValueError(format + " is not supported for responses with multiple layers")

    def get_tiles(self, format):
        unknown_layers = set(self.names) - set(self.config.layers.keys())
    
        if unknown_layers:
            raise KnownUnknown("%s.get_tiles didn't recognize %s when trying to load %s." % (__name__, ', '.join(unknown_layers), ', '.join(self.names)))
        
        layers = [self.config.layers[name] for name in self.names]
        mimes, bodies = zip(*[getTile(layer, self.coord, format.lower()) for layer in layers])
        bad_mimes = [(name, mime) for (mime, name) in zip(mimes, self.names) if not mime.endswith('/json')]
        
        if bad_mimes:
            raise KnownUnknown('%s.get_tiles encountered a non-JSON mime-type in %s sub-layer: "%s"' % ((__name__, ) + bad_mimes[0]))
        
        tiles = map(json.loads, bodies)
        bad_types = [(name, topo['type']) for (topo, name) in zip(tiles, self.names) if topo['type'] != ('FeatureCollection' if (format.lower()=='json') else 'Topology')]
        
        if bad_types:
            raise KnownUnknown('%s.get_tiles encountered a non-%sCollection type in %s sub-layer: "%s"' % ((__name__, ('Feature' if (format.lower()=='json') else 'Topology'), ) + bad_types[0]))
        
        return tiles


def query_columns(dbinfo, srid, subquery, bounds):
    ''' Get information about the columns returned for a subquery.
    '''
    with Connection(dbinfo) as db:
        #
        # While bounds covers less than the full planet, look for just one feature.
        #
        while (abs(bounds[2] - bounds[0]) * abs(bounds[2] - bounds[0])) < 1.61e15:
            bbox = 'ST_MakeBox2D(ST_MakePoint(%f, %f), ST_MakePoint(%f, %f))' % bounds
            bbox = 'ST_SetSRID(%s, %d)' % (bbox, srid)
        
            query = subquery.replace('!bbox!', bbox)
        
            db.execute(query + '\n LIMIT 1') # newline is important here, to break out of comments.
            row = db.fetchone()
            
            if row is None:
                #
                # Try zooming out three levels (8x) to look for features.
                #
                bounds = (bounds[0] - (bounds[2] - bounds[0]) * 3.5,
                          bounds[1] - (bounds[3] - bounds[1]) * 3.5,
                          bounds[2] + (bounds[2] - bounds[0]) * 3.5,
                          bounds[3] + (bounds[3] - bounds[1]) * 3.5)
                
                continue
            
            column_names = set(row.keys())
            return column_names

def get_features(dbinfo, query):
    with Connection(dbinfo) as db:
        db.execute(query)
        
        features = []
        
        for row in db.fetchall():
            if row['__geometry__'] is None:
                continue
        
            wkb = bytes(row['__geometry__'])
            prop = dict([(k, v) for (k, v) in row.items()
                         if (k not in ('__geometry__', '__id__') and v is not None)])
            
            if '__id__' in row:
                features.append((wkb, prop, row['__id__']))
            
            else:
                features.append((wkb, prop))
    return features

def build_query(srid, subquery, subcolumns, bounds, tolerance, is_geo, is_clipped, padding=0, scale=None):
    ''' Build and return an PostGIS query.
    '''
    bbox = 'ST_MakeBox2D(ST_MakePoint(%.2f, %.2f), ST_MakePoint(%.2f, %.2f))' % (bounds[0] - padding, bounds[1] - padding, bounds[2] + padding, bounds[3] + padding)
    bbox = 'ST_SetSRID(%s, %d)' % (bbox, srid)
    geom = 'q.__geometry__'
    
    if is_clipped:
        geom = 'ST_Intersection(%s, %s)' % (geom, bbox)
    
    if tolerance is not None:
        geom = 'ST_SimplifyPreserveTopology(%s, %.2f)' % (geom, tolerance)
    
    if is_geo:
        geom = 'ST_Transform(%s, 4326)' % geom

    if scale:
        # scale applies to the un-padded bounds, e.g. geometry in the padding area "spills over" past the scale range
        geom = 'ST_TransScale(%s, %.2f, %.2f, (%.2f / (%.2f - %.2f)), (%.2f / (%.2f - %.2f)))' % (geom, -bounds[0], -bounds[1], scale, bounds[2], bounds[0], scale, bounds[3], bounds[1])

    subquery = subquery.replace('!bbox!', bbox)
    columns = ['q."%s"' % c for c in subcolumns if c not in ('__geometry__', )]
    
    if '__geometry__' not in subcolumns:
        raise Exception("There's supposed to be a __geometry__ column.")
    
    if '__id__' not in subcolumns:
        columns.append('Substr(MD5(ST_AsBinary(q.__geometry__)), 1, 10) AS __id__')
    
    columns = ', '.join(columns)
    
    return '''SELECT %(columns)s,
                     ST_AsBinary(%(geom)s) AS __geometry__
              FROM (
                %(subquery)s
                ) AS q
              WHERE ST_IsValid(q.__geometry__)
                AND q.__geometry__ && %(bbox)s
                AND ST_Intersects(q.__geometry__, %(bbox)s)''' \
            % locals()
