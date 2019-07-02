from pyeo import validation
from pyeo.core import init_log
import numpy as np
import gdal
import os
import pytest


def setup_module():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    init_log("validation_test_log.log")


@pytest.mark.hi_mem
def test_stratified_random_sample():
    image_path = r"test_data/class_composite_T36MZE_20190509T073621_20190519T073621.tif"
    points = validation.stratified_random_sample(
        map_path = image_path,
        n_points = 100,
        no_data=0
    )
    assert len(points) == 100
    assert len(points[50]) == 2


@pytest.mark.hi_mem
def test_produce_stratifed_validation_points():
    image_path = r"test_data/class_composite_T36MZE_20190509T073621_20190519T073621.tif"
    out_path = r"test_outputs/strat_sample_test/strat_sample_test.shp"
    validation.produce_stratifed_validation_points(image_path, 500, out_path, no_data=0)


@pytest.mark.hi_mem
def test_get_class_point_lists():
    image_array = np.array([0, 1, 2, 4]*600)
    image_array = np.reshape(image_array, (600, 4))
    class_point_dict = validation.build_class_dict(image_array)
    assert class_point_dict
    print(class_point_dict)


def test_convert_point_list_to_shapefile():
    image_path = r"test_data/class_composite_T36MZE_20190509T073621_20190519T073621.tif"
    out_path = r"test_outputs/conversion_test/conversion_test.shp"
    point_list = [
        (0,50),
        (2000, 0),
        (1000, 500),
        (750, 2000)
    ]
    image = gdal.Open(image_path)
    gt = image.GetGeoTransform()
    proj = image.GetProjection()
    validation.save_point_list_to_shapefile(point_list, out_path, gt, proj)
    assert os.path.exists(out_path)


def test_point_allocation():
    #TODO: Get some working numbers for this from Qing.

