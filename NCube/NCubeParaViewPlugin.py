# -*- coding: utf-8 -*-
# Copyright (c) 2020 Alexey Pechnikov. All rights reserved.
# https://orcid.org/0000-0001-9626-8615 (ORCID)
# pechnikov@mobigroup.ru (email)
# License: http://opensource.org/licenses/MIT

from paraview.util.vtkAlgorithm import *
#from vtk.util.vtkAlgorithm import VTKPythonAlgorithmBase

def _str(text):
    import sys
    # fix string issue for Python 2
    if sys.version_info < (3, 0) and hasattr(text, 'encode'):
        return text.encode('utf-8')
    return str(text)

# Load shapefile or geojson
def _NCubeGeoDataFrameLoad(shapename, shapecol=None, shapeencoding=None):
    import geopandas as gpd
    if shapename is None:
        return
    df = gpd.read_file(shapename, encoding=shapeencoding)
    # clean undefined geometries
    df = df[df.geometry.notnull()]
    if (len(df)) == 0:
        return []
    if shapecol is not None:
        df = df.sort_values(shapecol).set_index(shapecol)
    #print ("shapecol",shapecol)
    return df

# load DEM
def _NCubeRasterLoad(rastername):
    import xarray as xr
    import numpy as np
    if rastername is None:
        return
    raster = xr.open_rasterio(rastername).squeeze()
    raster.values[raster.values == raster.nodatavals[0]] = np.nan

    return raster

def _NCubeGeoDataFrameToRaster(df, dem):
    import geopandas as gpd
    from shapely.geometry import Polygon
    
    if df is None or dem is None:
        return (df, dem)

    # extract EPSG code for raster
    dem_epsg = None
    if 'crs' in dem.attrs.keys():
        dem_epsg = int(dem.crs.split(':')[1])
    
    # extract EPSG code for geometry
    df_epsg = None
    if df.crs != {}:
        df_epsg = int(df.crs['init'].split(':')[1])
    
    #print ("epsg", dem_epsg, df_epsg)
    
    # crop geometries by the topography extent
    (xmin,xmax,ymin,ymax) = (dem.x.min(),dem.x.max(),dem.y.min(),dem.y.max())
    extent = Polygon([(xmin,ymin),(xmax,ymin),(xmax,ymax),(xmin,ymax)])
    #print ("extent", extent)
    df_extent = gpd.GeoDataFrame([],
                                 crs={'init' :'epsg:'+str(dem_epsg)} if dem_epsg is not None else {},
                                 geometry=[extent])
    #print (df_extent)
    # reproject when the both coordinate systems are defined and these are different
    if df_epsg and dem_epsg:
        df_extent_reproj = df_extent.to_crs(epsg=df_epsg).copy()
        #print ("df_extent valid",df_extent_reproj.geometry[0].is_valid)

        # if original or reprojected raster extent is valid, use it to crop geometry
        if df_extent_reproj.geometry[0].is_valid:
            # geometry intersection to raster extent in geometry coordinate system
            df = df[df.geometry.intersects(df_extent_reproj.geometry[0])]

    # reproject [cropped] geometry to original raster coordinates if needed
    if df_epsg and dem_epsg:
        df = df.to_crs(epsg=dem_epsg)
        # fix for broken [reprojected] geometries
        df = df[df.geometry.is_valid==True].copy()

    # crop raster by geometry extent
    (xmin,ymin,xmax,ymax) = df.geometry.total_bounds
    #print ("xmin,ymin,xmax,ymax",xmin,ymin,xmax,ymax)
    # for single point the extent is single point which produces empty raster
    if len(dem.x) > 1:
        xres = dem.x[1] - dem.x[0]
    else:
        xres = 1
    if len(dem.y) > 1:
        yres = dem.y[1] - dem.y[0]
    else:
        yres = 1
    dem = dem.sel(
            x=slice(xmin-xres if xres>0 else xmax-xres, xmax+xres if xres>0 else xmin+xres),
            y=slice(ymin-yres if yres>0 else ymax-yres, ymax+yres if yres>0 else ymin+yres)
        )
    #print ("x slice ", float(xmin-xres if xres>0 else xmax-xres), float(xmax+xres if xres>0 else xmin+xres))
    #print ("y slice ", float(ymin-yres if yres>0 else ymax-yres), float(ymax+yres if yres>0 else ymin+yres))
    #print ("dem", dem)
    
    # renumerate geometries
    return (df, dem)


# list of list of VtkArray's
def _NCubeGeoDataFrameRowToVTKArrays(row):
    #vtkPolyData, vtkAppendPolyData, vtkPoints, vtkCellArray, 
    from vtk import vtkStringArray, vtkIntArray, vtkFloatArray, vtkBitArray
    from shapely.geometry.base import BaseGeometry, BaseMultipartGeometry
    
    vtk_row = []
    for (key,value) in row.to_dict().items():
        #print (key,value)
        # define attribute as array
        if isinstance(value, (BaseMultipartGeometry)):
            #print ('BaseMultipartGeometry')
            continue
        elif isinstance(value, (BaseGeometry)):
            #print ('BaseGeometry')
            continue
        elif isinstance(value, (int)):
            vtk_arr = vtkIntArray()
        elif isinstance(value, (float)):
            vtk_arr = vtkFloatArray()
        elif isinstance(value, (bool)):
            vtk_arr = vtkBitArray()
        else:
            # some different datatypes could be saved as strings
            value = _str(value)
            vtk_arr = vtkStringArray()

        vtk_arr.SetNumberOfComponents(1)
        vtk_arr.SetName(key)
        vtk_row.append((vtk_arr, value))
    return vtk_row


def _NCubeGeometryToPolyData(geometry, dem):
    from vtk import vtkPolyData, vtkAppendPolyData, vtkPoints, vtkCellArray, vtkStringArray, vtkIntArray, vtkFloatArray, vtkBitArray
    from shapely.geometry.base import BaseGeometry, BaseMultipartGeometry
    import xarray as xr
    import numpy as np
    
    if isinstance(geometry, (BaseMultipartGeometry)):
    #if row.geometry.geometryType()[:5] == 'Multi':
        geoms = geometry.geoms
    else:
        geoms = [geometry]

    vtk_points = vtkPoints()
    vtk_cells = vtkCellArray()
    # iterate parts of (multi)geometry
    for geom in geoms:
        if geom.type == 'Polygon':
            # use exterior coordinates only
            coords = geom.exterior.coords
        else:
            coords = geom.coords

        count = (len(coords))
        #print ("count",count)
        vtk_cells.InsertNextCell(count)

        xs = coords.xy[0]
        ys = coords.xy[1]
        if len(coords.xy) > 2:
            zs = coords.xy[2]
        else:
            zs = [0]*len(xs)
        # find nearest raster values for every geometry point
        if dem is not None:
            zs = dem.sel(x=xr.DataArray(xs), y=xr.DataArray(ys), method='nearest').values
            # code below should be faster for large geometries
#            yy = dem.y.sel(y=xr.DataArray(ys), method='nearest').values
#            xx = dem.x.sel(x=xr.DataArray(xs), method='nearest').values
#            zs = dem.sel(x=xr.DataArray(xx), y=xr.DataArray(yy)).values
        for (x,y,z) in zip(xs,ys,zs):
            pointId = vtk_points.InsertNextPoint(x, y, z)
            vtk_cells.InsertCellPoint(pointId)

    vtk_polyData = vtkPolyData()
    vtk_polyData.SetPoints(vtk_points)
    if geom.type == 'Point':
        vtk_polyData.SetVerts(vtk_cells)
    else:
        vtk_polyData.SetLines(vtk_cells)

    return vtk_polyData


def _NCubeGeometryOnTopography(shapename, toponame, shapecol, shapeencoding):
    from vtk import vtkPolyData, vtkAppendPolyData, vtkPoints, vtkCellArray, vtkStringArray, vtkIntArray, vtkFloatArray, vtkBitArray
    from shapely.geometry.base import BaseGeometry, BaseMultipartGeometry
    import numpy as np
    
    #print ("_NCUBEGeometryOnTopography start")

    # Load shapefile
    if shapename is None:
        return

    df = _NCubeGeoDataFrameLoad(shapename, shapecol, shapeencoding)
    if df is None or len(df) == 0:
            return

    # load DEM
    dem = dem_values = None
    if toponame is not None:
        dem = _NCubeRasterLoad(toponame)
    # crop geometry and topography together
    (df, dem) = _NCubeGeoDataFrameToRaster(df, dem)
    
    groups = df.index.unique()
    #print ("groups",groups)
    
    # iterate blocks
    vtk_blocks = []
    for group in groups:
        #print ("group",group)
        # Python 2 string issue wrapped
        if hasattr(group, 'encode'):
            _df = df[df.index.str.match(group)].reset_index()
        else:
            _df = df[df.index == group].reset_index()
        vtk_appendPolyData = vtkAppendPolyData()
        # iterate rows
        for rowidx,row in _df.iterrows():
            vtk_polyData = _NCubeGeometryToPolyData(row.geometry, dem)
            vtk_arrays = _NCubeGeoDataFrameRowToVTKArrays(row)
            for (vtk_arr, val) in vtk_arrays:
                for _ in range(vtk_polyData.GetNumberOfCells()):
                    vtk_arr.InsertNextValue(val)
                vtk_polyData.GetCellData().AddArray(vtk_arr)
            # compose vtkPolyData
            vtk_appendPolyData.AddInputData(vtk_polyData)
        vtk_appendPolyData.Update()
        vtk_block = vtk_appendPolyData.GetOutput()

        vtk_blocks.append((_str(group),vtk_block))

    #print ("_NCUBEGeometryOnTopography end")

    return vtk_blocks

#------------------------------------------------------------------------------
# A source example.
#------------------------------------------------------------------------------
@smproxy.source(name="NCubeGeometryOnTopographySource",
       label="N-Cube Shapefile On Topography Source")
class NCubeGeometryOnTopographySource(VTKPythonAlgorithmBase):
    def __init__(self):
        VTKPythonAlgorithmBase.__init__(self,
                nInputPorts=0,
                nOutputPorts=1,
                outputType='vtkPolyData')
        self._shapename = None
        self._shapeencoding = None
        self._shapecol = None
        self._toponame = None


    def RequestData(self, request, inInfo, outInfo):
        from vtk import vtkPolyData, vtkAppendPolyData
        import time
        
        if self._shapename is None:
            return 1

        t0 = time.time()
        vtk_blocks = _NCubeGeometryOnTopography(self._shapename, self._toponame, self._shapecol, self._shapeencoding)
        if vtk_blocks == []:
            return
    
        vtk_polyDatas = vtkAppendPolyData()
        for (label, vtk_polyData) in vtk_blocks:
            vtk_polyDatas.AddInputData(vtk_polyData)
        vtk_polyDatas.Update()
        vtkPolyData.GetData(outInfo, 0).ShallowCopy(vtk_polyDatas.GetOutput())
        t1 = time.time()
        print ("t1-t0", t1-t0)

        return 1


    @smproperty.stringvector(name="Shapefile Name")
    @smdomain.filelist()
    @smhint.filechooser(extensions=["shp", "geojson"], file_description="ESRI Shapefile, GeoJSON")
    def SetShapeFileName(self, name):
        """Specify filename for the shapefile to read."""
        print ("SetShapeFileName", name)
        name = name if name != 'None' else None
        if self._shapename != name:
            self._shapename = name
            self._shapecol = None
            self.Modified()

    @smproperty.stringvector(name="Topography File Name (optional)")
    @smdomain.filelist()
    @smhint.filechooser(extensions=["tif", "TIF", "nc"], file_description="GeoTIFF, NetCDF")
    def SetTopographyFileName(self, name):
        """Specify filename for the topography file to read."""
        print ("SetTopographyFileName", name)
        name = name if name != 'None' else None
        if self._toponame != name:
            self._toponame = name
            self.Modified()





@smproxy.source(name="NCubeGeometryOnTopographyBlockSource",
       label="N-Cube Shapefile On Topography Block Source")
class NCubeGeometryOnTopographyBlockSource(VTKPythonAlgorithmBase):
    def __init__(self):
        VTKPythonAlgorithmBase.__init__(self,
                nInputPorts=0,
                nOutputPorts=1,
                outputType='vtkMultiBlockDataSet')
        self._shapename = None
        self._shapeencoding = None
        self._shapecol = None
        self._toponame = None


    def RequestData(self, request, inInfo, outInfo):
        from vtk import vtkPolyData, vtkAppendPolyData, vtkCompositeDataSet, vtkMultiBlockDataSet
        
        if self._shapename is None:
            return 1

        vtk_blocks = _NCubeGeometryOnTopography(self._shapename, self._toponame, self._shapecol, self._shapeencoding)
        if vtk_blocks == []:
            return
        print ("vtk_blocks", len(vtk_blocks))
        mb = vtkMultiBlockDataSet.GetData(outInfo, 0)
        mb.SetNumberOfBlocks(len(vtk_blocks))
        rowidx = 0
        for (label, polyData) in vtk_blocks:
            #print (rowidx, label)
            mb.SetBlock( rowidx, polyData )
            mb.GetMetaData( rowidx ).Set( vtkCompositeDataSet.NAME(), label)
            rowidx += 1
        return 1


    @smproperty.stringvector(name="Shapefile Name")
    @smdomain.filelist()
    @smhint.filechooser(extensions=["shp", "geojson"], file_description="ESRI Shapefile, GeoJSON")
    def SetShapeFileName(self, name):
        """Specify filename for the shapefile to read."""
        print ("SetShapeFileName", name)
        name = name if name != 'None' else None
        if self._shapename != name:
            self._shapename = name
            self._shapecol = None
            self.Modified()

    @smproperty.stringvector(name="Topography File Name (optional)")
    @smdomain.filelist()
    @smhint.filechooser(extensions=["tif", "TIF", "nc"], file_description="GeoTIFF, NetCDF")
    def SetTopographyFileName(self, name):
        """Specify filename for the topography file to read."""
        print ("SetTopographyFileName", name)
        name = name if name != 'None' else None
        if self._toponame != name:
            self._toponame = name
            self.Modified()

    @smproperty.stringvector(name="ShapeLabels", information_only="1")
    def ShapeLabels(self):
        if self._shapename is None:
            return []
        # Load shapefile
        import geopandas as gpd
        df = gpd.read_file(self._shapename, encoding=self._shapeencoding)
        cols = sorted(df.columns.values)
        del df
        if 'geometry' in cols:
            cols.remove('geometry')
        #print(cols)
        return ['None'] + list(map(str,cols))

    @smproperty.stringvector(name="Group by Field (optional)", number_of_elements="1")
    @smdomain.xml(\
        """<StringListDomain name="list">
                <RequiredProperties>
                    <Property name="ShapeLabels" function="GetShapeLabels"/>
                </RequiredProperties>
            </StringListDomain>
        """)
    def SetShapeLabel(self, label):
        label = label if label != 'None' else None
        self._shapecol = label
        print("SetShapeLabel ", label)
        self.Modified()









@smproxy.source(name="NCUBETopographySource",
       label="N-Cube Topography Source")
class NCUBETopographySource(VTKPythonAlgorithmBase):
    def __init__(self):
        VTKPythonAlgorithmBase.__init__(self,
                nInputPorts=0,
                nOutputPorts=1,
                outputType='vtkMultiBlockDataSet')
        self._shapename = None
        self._shapeencoding = None
        self._shapecol = None
        self._toponame = None

    def RequestData(self, request, inInfo, outInfo):
        return 1


    @smproperty.stringvector(name="Shapefile Name (optional)")
    @smdomain.filelist()
    @smhint.filechooser(extensions=["shp", "geojson"], file_description="ESRI Shapefile, GeoJSON")
    def SetShapeFileName(self, name):
        """Specify filename for the shapefile to read."""
        print ("SetShapeFileName", name)
        name = name if name != 'None' else None
        if self._shapename != name:
            self._shapename = name
            self._shapecol = None
            self._ndata = None
            self._timesteps = None
            self.Modified()

    @smproperty.stringvector(name="Topography File Name")
    @smdomain.filelist()
    @smhint.filechooser(extensions=["tif", "TIF", "nc"], file_description="GeoTIFF, NetCDF")
    def SetTopographyFileName(self, name):
        """Specify filename for the topography file to read."""
        print ("SetTopographyFileName", name)
        name = name if name != 'None' else None
        if self._toponame != name:
            self._toponame = name
            self.Modified()

    @smproperty.stringvector(name="Shapefile Label Attribute (optional)")
    def SetShapeLabel(self, name):
        self.Modified()

    @smproperty.stringvector(name="ShapeLabels", information_only="1")
    def ShapeLabels(self):
        if self._shapename is None:
            return []
        # Load shapefile
        import geopandas as gpd
        df = gpd.read_file(self._shapename, encoding=self._shapeencoding)
        cols = sorted(df.columns.values)
        if 'geometry' in cols:
            cols.remove('geometry')
        del df
        #print(cols)
        return ['None'] + list(map(str,cols))

    @smproperty.stringvector(name="Shapefile Label Attribute", number_of_elements="1")
    @smdomain.xml(\
        """<StringListDomain name="list">
                <RequiredProperties>
                    <Property name="ShapeLabels" function="GetShapeLabels"/>
                </RequiredProperties>
            </StringListDomain>
        """)
    def SetShapeLabel(self, label):
        label = label if label != 'None' else None
        self._shapecol = label
        print("SetShapeLabel ", label)
        self.Modified()








# TODO
#@smproxy.source(name="NCUBEImageOnTopographySource",
#       label="N-Cube Image On Topography Source")
