"""
pyeo.classification
===================
Contains every function to do with map classification. This includes model creation, map classification and processes
for array manipulation into scikit-learn compatible forms.

For details on how to build a class shapefile, see :ref:CLASSIFICATION INSTRUCTIONS

All models are serialised and deserialised using :code:`joblib.dump` or :code:`joblib.load`, and saved with the .pkl
extension

Key functions
-------------

:py:func:`extract_features_to_csv` Extracts class signatures from a class shapefile and a .tif

:py:func:`create_model_from_signatures` Creates a model from a .csv of classes and band signatures

:py:func:`create_trained_model` Creates a model from a class shapefile and a .tif

:py:func:`classify_image` Produces a classification map from an image using a model.

Function reference
------------------
"""
import csv
import glob
import logging
import os
from tempfile import TemporaryDirectory

from osgeo import gdalconst
from osgeo import gdal
from osgeo import osr
from osgeo import ogr
import joblib
import numpy as np
from scipy import sparse as sp
import shutil
from sklearn import ensemble as ens
from sklearn.externals import joblib as sklearn_joblib
from sklearn.model_selection import cross_val_score

from pyeo.coordinate_manipulation import get_local_top_left
from pyeo.filesystem_utilities import get_mask_path
from pyeo.raster_manipulation import stack_images, create_matching_dataset, apply_array_image_mask, get_masked_array

import pyeo.windows_compatability

gdal.UseExceptions()

log = logging.getLogger(__name__)


def change_from_composite(image_path, composite_path, model_path, class_out_path, prob_out_path=None, skip_existing=False, apply_mask=False):
    """
    Stacks an image with a composite and classifies each pixel change with a scikit-learn model.

    The image that is classified is has the following bands

    1. composite blue
    2. composite green
    3. composite red
    4. composite IR
    5. image blue
    6. image green
    7. image red
    8. image IR

    Parameters
    ----------
    image_path : str
        The path to the image
    composite_path : str
        The path to the composite
    model_path : str
        The path to a .pkl of a scikit-learn classifier that takes 8 features
    class_out_path : str
        A location to save the resulting classification .tif
    prob_out_path : str, optional
        A location to save the probability raster of each pixel.
    skip_existing : bool, optional
        If true, do not run if class_out_path already exists. Defaults to False.
    apply_mask : bool, optional
        If True, uses the .msk file corresponding to the image at image_path to skip any invalid pixels. Default False.
    """

    if skip_existing:
        if os.path.exists(class_out_path):
            log.info(" Classified image exists. Skipping. {}".format(class_out_path))
            return
    if os.path.exists(composite_path):
        if os.path.exists(image_path):
            with TemporaryDirectory(dir=os.getcwd()) as td:
                stacked_path = os.path.join(td, "comp_stack.tif")
                log.info("stacked path: {}".format(stacked_path))
                stack_images([composite_path, image_path], stacked_path)
                log.info(" stacked path exists? {}".format(os.path.exists(stacked_path)))
                classify_image(stacked_path, model_path, class_out_path, prob_out_path, apply_mask, skip_existing)
                log.info(" class out path exists? {}".format(os.path.exists(class_out_path)))
                return
        else:
            log.error("File not found: {}".format(image_path))
    else:
        log.error("File not found: {}".format(composite_path))
    return


def classify_image(image_path, model_path, class_out_path, prob_out_path=None, apply_mask=False,
                   out_format="GTiff", num_chunks=4, nodata=0, skip_existing = False):
    """

    Produces a class map from a raster and a model.

    This applies the model's fit() function to each pixel in the input raster, and saves the result into an output
    raster. The model is presumed to be a scikit-learn fitted model created using one of the other functions in this
    library (:py:func:`create_model_from_signatures` or :py:func:`create_trained_model`).

    Parameters
    ----------
    image_path : str
        The path to the raster image to be classified.
    model_path : str
        The path to the .pkl file containing the model
    class_out_path : str
        The path that the classified map will be saved at.
    prob_out_path : str, optional
        If present, the path that the class probability map will be stored at. Default None
    apply_mask : bool, optional
        If True, uses the .msk file corresponding to the image at image_path to skip any invalid pixels. Default False.
    out_type : str, optional
        The raster format of the class image. Defaults to "GTiff" (geotif). See gdal docs for valid types.
    num_chunks : int, optional
        The number of chunks the image is broken into prior to classification. The smaller this number, the faster
        classification will run - but the more likely you are to get a outofmemory error. Default 10.
    nodata : int, optional
        The value to write to masked pixels. Defaults to 0.
    skip_existing : bool, optional
        If true, do not run if class_out_path already exists. Defaults to False.

    Notes
    -----
    If you want to create a custom model, the object is presumed to have the following methods and attributes:

       - model.n_classes_ : the number of classes the model will produce
       - model.n_cores : The number of CPU cores used to run the model
       - model.predict() : A function that will take a set of band inputs from a pixel and produce a class.
       - model.predict_proba() : If called with prob_out_path, a function that takes a set of n band inputs from a pixel
                                and produces n_classes_ outputs corresponding to the probabilties of a given pixel being
                                that class

    """

    if skip_existing:
        log.info("Checking for existing classification {}".format(class_out_path))
        if os.path.isfile(class_out_path):
            log.info("Class image exists, skipping.")
            return class_out_path
    log.info("Classifying file: {}".format(image_path))
    log.info("Saved model     : {}".format(model_path))
    if not os.path.exists(image_path):
        log.error("File not found: {}".format(image_path))
    if not os.path.exists(model_path):
        log.error("File not found: {}".format(model_path))
    try:
        image = gdal.Open(image_path)
    except RuntimeError as e:
        log.info("Exception: {}".format(e))
        exit(1)
    if num_chunks == None:
        log.info("No chunk size given, attempting autochunk.")
        num_chunks = autochunk(image)
        log.info("Autochunk to {} chunks".format(num_chunks))
    try:
        model = sklearn_joblib.load(model_path)
    except KeyError as e:
        log.warning("Sklearn joblib import failed,trying generic joblib: {}".format(e))
        model = joblib.load(model_path)
    except TypeError as e:
        log.warning("Sklearn joblib import failed,trying generic joblib: {}".format(e))
        model = joblib.load(model_path)
    class_out_image = create_matching_dataset(image, class_out_path, format=str(out_format), datatype=gdal.GDT_Byte)
    log.info("Created classification image file: {}".format(class_out_path))
    if prob_out_path:
        try:
            log.info("n classes in the model: {}".format(model.n_classes_))
        except AttributeError as e:
            log.warning("Model has no n_classes_ attribute (known issue with GridSearch): {}".format(e))
        prob_out_image = create_matching_dataset(image, prob_out_path, bands=model.n_classes_, datatype=gdal.GDT_Float32)
        log.info("Created probability image file: {}".format(prob_out_path))
    model.n_cores = -1
    image_array = image.GetVirtualMemArray()

    if apply_mask:
        mask_path = get_mask_path(image_path)
        log.info("Applying mask at {}".format(mask_path))
        mask = gdal.Open(mask_path)
        mask_array = mask.GetVirtualMemArray()
        image_array = apply_array_image_mask(image_array, mask_array)
        mask_array = None
        mask = None

    # Mask out missing values from the classification
    # at this point, image_array has dimensions [band, y, x]
    image_array = reshape_raster_for_ml(image_array)
    # Now it has dimensions [x * y, band] as needed for Scikit-Learn

    # Determine where in the image array there are no missing values in any of the bands (axis 1)
    #log.info("Finding good pixels without missing values")
    #log.info("image_array.shape = {}".format(image_array.shape))
    n_samples = image_array.shape[0]  # gives x * y dimension of the whole image
    nbands = image_array.shape[1] # gives number of bands
    boo = np.where(image_array[:,0] != nodata, True, False)
    if nbands > 1:
        for band in range(1, nbands, 1):
            boo1 = np.where(image_array[:,band] != nodata, True, False)
            boo = np.logical_and(boo, boo1)
    good_indices = np.where(boo)[0] # get indices where all bands contain data
    good_sample_count = np.count_nonzero(boo)
    log.info("Proportion of non-missing values: {}%".format(good_sample_count/n_samples*100))
    good_samples = np.take(image_array, good_indices, axis=0).squeeze()
    n_good_samples = len(good_samples)
    classes = np.full(n_good_samples, nodata, dtype=np.ubyte)
    if prob_out_path:
        probs = np.full((n_good_samples, model.n_classes_), nodata, dtype=np.float32)
    chunk_size = int(n_good_samples / num_chunks)
    chunk_resid = n_good_samples - (chunk_size * num_chunks)
    log.info("   Number of chunks {} Chunk size {} Chunk residual {}".format(num_chunks, chunk_size, chunk_resid))
    # The chunks iterate over all values in the array [x * y, bands] always with all bands per chunk
    for chunk_id in range(num_chunks):
        offset = chunk_id * chunk_size
        if chunk_id == num_chunks - 1:
            chunk_size = chunk_size + chunk_resid
        log.info("   Classifying chunk {} of size {}".format(chunk_id+1, chunk_size))
        chunk_view = good_samples[offset : offset + chunk_size]
        indices_view = good_indices[offset : offset + chunk_size]
        out_view = classes[offset : offset + chunk_size]
        chunk_view = chunk_view.copy() # bug fix for Pandas bug: https://stackoverflow.com/questions/53985535/pandas-valueerror-buffer-source-array-is-read-only
        indices_view = indices_view.copy() # bug fix for Pandas bug: https://stackoverflow.com/questions/53985535/pandas-valueerror-buffer-source-array-is-read-only
        out_view[:] = model.predict(chunk_view)
        if prob_out_path:
            log.info("   Calculating probabilities")
            prob_view = probs[offset : offset + chunk_size, :]
            prob_view[:, :] = model.predict_proba(chunk_view)

    class_out_array = np.full((n_samples), nodata)
    for i, class_val in zip(good_indices, classes):
        class_out_array[i] = class_val
    class_out_image.GetVirtualMemArray(eAccess=gdal.GF_Write)[:, :] = \
        reshape_ml_out_to_raster(class_out_array, image.RasterXSize, image.RasterYSize)

    if prob_out_path:
        #log.info("   Creating probability array of size {}".format(n_samples * model.n_classes_))
        prob_out_array = np.full((n_samples, model.n_classes_), nodata)
        for i, prob_val in zip(good_indices, probs):
            prob_out_array[i] = prob_val
        #log.info("   Creating GDAL probability image")
        #log.info("   N Classes = {}".format(prob_out_array.shape[1]))
        #log.info("   Image X size = {}".format(image.RasterXSize))
        #log.info("   Image Y size = {}".format(image.RasterYSize))
        prob_out_image.GetVirtualMemArray(eAccess=gdal.GF_Write)[:, :, :] = \
            reshape_prob_out_to_raster(prob_out_array, image.RasterXSize, image.RasterYSize)

    class_out_image = None
    class_out_array = None
    prob_out_image = None
    prob_out_array = None
    # verify that the output file(s) have been created
    if not os.path.exists(class_out_path):
        log.error("File not found: {}".format(class_out_path))
        sys.exit(1)
    if prob_out_path:
        if not os.path.exists(prob_out_path):
            log.error("File not found: {}".format(prob_out_path))
            sys.exit(1)
        return class_out_path, prob_out_path
    else:
        return class_out_path


def classify_image_and_composite(image_path, composite_path, model_path, class_out_path, prob_out_path=None,
                   apply_mask=False, out_type="GTiff", num_chunks=10, nodata=0, skip_existing = False):
    """
    !!! WARNING - currently does nothing. Not successfully tested yet. Use change_from_composite() function instead.

    Produces a class map from a raster file, a composite raster file and a model.
    This applies the model's fit() function to each pixel in the input raster, and saves the result into an output
    raster. The model is presumed to be a scikit-learn fitted model created using one of the other functions in this
    library (:py:func:`create_model_from_signatures` or :py:func:`create_trained_model`).

    Parameters
    ----------
    image_path : str
        The path to the raster image to be classified.
    composite_path : str
        The path to the raster image composite to be used as a baseline.
    model_path : str
        The path to the .pkl file containing the model
    class_out_path : str
        The path that the classified map will be saved at.
    prob_out_path : str, optional
        If present, the path that the class probability map will be stored at. Default None
    apply_mask : bool, optional
        If True, uses the .msk file corresponding to the image at image_path to skip any invalid pixels. Default False.
    out_type : str, optional
        The raster format of the class image. Defaults to "GTiff" (geotif). See gdal docs for valid types.
    num_chunks : int, optional
        The number of chunks the image is broken into prior to classification. The smaller this number, the faster
        classification will run - but the more likely you are to get a outofmemory error. Default 10.
    nodata : int, optional
        The value to write to masked pixels. Defaults to 0.
    skip_existing : bool, optional
        If true, do not run if class_out_path already exists. Defaults to False.


    Notes
    -----
    If you want to create a custom model, the object is presumed to have the following methods and attributes:

       - model.n_classes_ : the number of classes the model will produce
       - model.n_cores : The number of CPU cores used to run the model
       - model.predict() : A function that will take a set of band inputs from a pixel and produce a class.
       - model.predict_proba() : If called with prob_out_path, a function that takes a set of n band inputs from a pixel
                                and produces n_classes_ outputs corresponding to the probabilties of a given pixel being
                                that class

    if skip_existing:
        log.info("Checking for existing classification {}".format(class_out_path))
        if os.path.isfile(class_out_path):
            log.info("Class image exists, skipping.")
            return class_out_path
    log.info("Classifying file: {}".format(image_path))
    log.info("Saved model     : {}".format(model_path))
    image = gdal.Open(image_path)
    composite = gdal.Open(composite_path)
    if num_chunks == None:
        log.info("No chunk size given, attempting autochunk.")
        num_chunks = autochunk(image)
        log.info("Autochunk to {} chunks".format(num_chunks))
    try:
        model = sklearn_joblib.load(model_path)
    except KeyError:
        log.warning("Sklearn joblib import failed,trying generic joblib")
        model = joblib.load(model_path)
    except TypeError:
        log.warning("Sklearn joblib import failed,trying generic joblib")
        model = joblib.load(model_path)
    class_out_image = create_matching_dataset(image, class_out_path, format=out_type, datatype=gdal.GDT_Byte)
    log.info("Created classification image file: {}".format(class_out_path))
    if prob_out_path:
        try:
            log.info("n classes in the model: {}".format(model.n_classes_))
        except AttributeError:
            log.warning("Model has no n_classes_ attribute (known issue with GridSearch)")
        prob_out_image = create_matching_dataset(image, prob_out_path, bands=model.n_classes_, datatype=gdal.GDT_Float32)
        log.info("Created probability image file: {}".format(prob_out_path))
    model.n_cores = -1
    image_array = image.GetVirtualMemArray()
    composite_array = composite.GetVirtualMemArray()

    if apply_mask:
        mask_path = get_mask_path(image_path)
        log.info("Applying mask at {}".format(mask_path))
        mask = gdal.Open(mask_path)
        mask_array = mask.GetVirtualMemArray()
        image_array = apply_array_image_mask(image_array, mask_array)
        mask_array = None
        mask = None

    # Mask out missing values from the classification
    # at this point, image_array has dimensions [band, y, x]
    image_array = reshape_raster_for_ml(image_array)
    composite_array = reshape_raster_for_ml(composite_array)
    # Now it has dimensions [x * y, band] as needed for Scikit-Learn
    log.info("Shape of composite array = {}".format(composite_array.shape))
    log.info("Shape of image array = {}".format(image_array.shape))

    # Determine where in the image array there are no missing values in any of the bands (axis 1)
    n_samples = image_array.shape[0]  # gives x * y dimension of the whole image
    good_mask = np.all(image_array != nodata, axis=1)
    good_sample_count = np.count_nonzero(good_mask)
    log.info("Number of good pixel values: {}".format(good_sample_count))
    if good_sample_count > 0:
        #TODO: if good_sample_count <= 0.5*len(good_mask):  # If the images is less than 50% good pixels, do filtering
        if 1 == 0:  # Removing the filter until we fix the classification issue with it
            log.info("Filtering nodata values")
            good_indices = np.nonzero(good_mask)
            good_samples = np.take(image_array, good_indices, axis=0).squeeze()
            n_good_samples = len(good_samples)
            log.info("Number of pixel values to be classified: {}".format(n_good_samples))
        else:
            #log.info("Not worth filtering nodata, skipping.")
            good_samples = np.concatenate((composite_array, image_array), axis=1)
            good_indices = range(0, n_samples)
            n_good_samples = n_samples
            log.info("Number of pixel values to be classified: {}".format(n_good_samples))
        log.info("Shape of good samples array = {}".format(good_samples.shape))
        classes = np.full(n_good_samples, nodata, dtype=np.ubyte)
        if prob_out_path:
            probs = np.full((n_good_samples, model.n_classes_), nodata, dtype=np.float32)

        chunk_size = int(n_good_samples / num_chunks)
        chunk_resid = n_good_samples - (chunk_size * num_chunks)
        log.info("   Number of chunks {}. Chunk size {}. Chunk residual {}.".format(num_chunks, chunk_size, chunk_resid))
        # The chunks iterate over all values in the array [x * y, bands] always with 8 bands per chunk
        for chunk_id in range(num_chunks):
            offset = chunk_id * chunk_size
            # process the residual pixels with the last chunk
            if chunk_id == num_chunks - 1:
                chunk_size = chunk_size + chunk_resid
            log.info("   Classifying chunk {} of size {}".format(chunk_id, chunk_size))
            chunk_view = good_samples[offset : offset + chunk_size]
            #indices_view = good_indices[offset : offset + chunk_size]
            #log.info("   Creating out_view")
            out_view = classes[offset : offset + chunk_size]  # dimensions [chunk_size]
            #log.info("   Calling model.predict")
            chunk_view = chunk_view.copy() # bug fix for Pandas bug: https://stackoverflow.com/questions/53985535/pandas-valueerror-buffer-source-array-is-read-only
            out_view[:] = model.predict(chunk_view)

            if prob_out_path:
                log.info("   Calculating probabilities")
                prob_view = probs[offset : offset + chunk_size, :]
                prob_view[:, :] = model.predict_proba(chunk_view)

        #log.info("   Creating class array of size {}".format(n_samples))
        class_out_array = np.full((n_samples), nodata)
        for i, class_val in zip(good_indices, classes):
            class_out_array[i] = class_val

        #log.info("   Creating GDAL class image")
        class_out_image.GetVirtualMemArray(eAccess=gdal.GF_Write)[:, :] = \
            reshape_ml_out_to_raster(class_out_array, image.RasterXSize, image.RasterYSize)

        if prob_out_path:
            #log.info("   Creating probability array of size {}".format(n_samples * model.n_classes_))
            prob_out_array = np.full((n_samples, model.n_classes_), nodata)
            for i, prob_val in zip(good_indices, probs):
                prob_out_array[i] = prob_val
            #log.info("   Creating GDAL probability image")
            #log.info("   N Classes = {}".format(prob_out_array.shape[1]))
            #log.info("   Image X size = {}".format(image.RasterXSize))
            #log.info("   Image Y size = {}".format(image.RasterYSize))
            prob_out_image.GetVirtualMemArray(eAccess=gdal.GF_Write)[:, :, :] = \
                reshape_prob_out_to_raster(prob_out_array, image.RasterXSize, image.RasterYSize)

        class_out_image = None
        prob_out_image = None
        if prob_out_path:
            return class_out_path, prob_out_path
        else:
            return class_out_path
    else:
        log.warning("No good pixels found - no classification image was created.")
        return ""
    """
    log.error("This function currently does nothing. Call pyeo.classification.change_from_composite instead.")
    return ""
    



def autochunk(dataset, mem_limit=None):
    """
    :meta private:
    EXPERIMENTAL Calculates the number of chunks to break a dataset into without a memory error. Presumes that 80% of the
    memory on the host machine is available for use by Pyeo.
    We want to break the dataset into as few chunks as possible without going over mem_limit.
    mem_limit defaults to total amount of RAM available on machine if not specified

    Parameters
    ----------
    dataset
        The dataset to chunk
    mem_limit
        The maximum amount of memory available to the process. Will be automatically populated from os.sysconf if missing.

    Returns
    -------
    The number of chunks to most efficiently break the image into.

    """
    pixels = dataset.RasterXSize * dataset.RasterYSize
    bytes_per_pixel = dataset.GetVirtualMemArray().dtype.itemsize*dataset.RasterCount
    image_bytes = bytes_per_pixel*pixels
    if not mem_limit:
        mem_limit = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_AVPHYS_PAGES')
        # Lets assume that 20% of memory is being used for non-map bits
        mem_limit = int(mem_limit*0.8)
    # if I went back now, I would fail basic programming here.
    for num_chunks in range(1, pixels):
        if pixels % num_chunks != 0:
            continue
        chunk_size_bytes = (pixels/num_chunks)*bytes_per_pixel
        if chunk_size_bytes < mem_limit:
            return num_chunks


def classify_directory(in_dir, model_path, class_out_dir, prob_out_dir = None,
                       apply_mask=False, out_type="GTiff", num_chunks=4, skip_existing=False):
    """
    Classifies every file ending in .tif in in_dir using model at model_path. Outputs are saved
    in class_out_dir and prob_out_dir, named [input_name]_class and _prob, respectively.

    See the documentation for classification.classify_image() for more details.


    Parameters
    ----------
    in_dir : str
        The path to the directory containing the rasters to be classified.
    model_path : str
        The path to the .pkl file containing the model.
    class_out_dir : str
        The directory that will store the classified maps
    prob_out_dir : str, optional
        If present, the directory that will store the probability maps of the classified maps. If not provided, will not generate probability maps.
    apply_mask : bool, optional
        If present, uses the corresponding .msk files to mask the directories. Defaults to True.
    out_type : str, optional
        The raster format of the class image. Defaults to "GTiff" (geotif). See gdal docs for valid datatypes.
    num_chunks : int, optional
        The number of chunks to break each image into for processing. See :py:func:`classify_image`
    skip_existing : boolean, optional
        If True, skips the classification if the output file already exists.
    """

    log = logging.getLogger(__name__)
    log.info("Classifying files in {}".format(in_dir))
    log.info("Class files saved in {}".format(class_out_dir))
    log.info("Prob. files saved in {}".format(prob_out_dir))
    log.info("Skip existing files? {}".format(skip_existing))
    for image_path in glob.glob(in_dir+r"/*.tif"):
        image_name = os.path.basename(image_path)[:-4]
        class_out_path = os.path.join(class_out_dir, image_name+"_class.tif")
        if prob_out_dir:
            prob_out_path = os.path.join(prob_out_dir, image_name+"_prob.tif")
        else:
            prob_out_path = None
        classify_image(image_path = image_path, 
                       model_path = model_path, 
                       class_out_path = class_out_path, 
                       prob_out_path = prob_out_path,
                       apply_mask = apply_mask, 
                       out_format = out_type, 
                       num_chunks = num_chunks, 
                       skip_existing=skip_existing)


def reshape_raster_for_ml(image_array):
    """
    A low-level function that reshapes an array from gdal order `[band, y, x]` to scikit features order `[x*y, band]`

    For classification, scikit-learn functions take a 2-dimensional array of features of the shape (samples, features).
    For pixel classification, features correspond to bands and samples correspond to specific pixels.

    Parameters
    ----------
    image_array : array_like
        A 3-dimensional Numpy array of shape (bands, y, x) containing raster data.

    Returns
    -------
    array_like
        A 2-dimensional Numpy array of shape (samples, features)

    """
    bands, y, x = image_array.shape
    image_array = np.transpose(image_array, (1, 2, 0))
    image_array = np.reshape(image_array, (x * y, bands))
    return image_array


def reshape_ml_out_to_raster(classes, width, height):
    """
    Takes the output of a pixel classifier and reshapes to a single band image.

    Parameters
    ----------
    classes : array_like of int
        A 1-d numpy array of classes from a pixel classifier
    width : int
        The width in pixels of the image the produced the classification
    height : int
        The height in pixels of the image that produced the classification

    Returns
    -------
        A 2-dimensional Numpy array of shape(width, height)

    """
    # TODO: Test this.
    image_array = np.reshape(classes, (height, width))
    return image_array


def reshape_prob_out_to_raster(probs, width, height):
    """
    Takes the probability output of a pixel classifier and reshapes it to a raster.

    Parameters
    ----------
    probs : array_like
        A numpy array of shape(n_pixels, n_classes)
    width : int
        The width in pixels of the image that produced the probability classification
    height : int
        The height in pixels of the image that produced the probability classification

    Returns
    -------
    array_like
        The reshaped image array

    """
    classes = probs.shape[1]
    image_array = np.transpose(probs, (1, 0))
    image_array = np.reshape(image_array, (classes, height, width))
    return image_array

def extract_features_to_csv(in_ras_path, training_shape_path, out_path, attribute="CODE"):
    """
    Given a raster and a shapefile containing training polygons, extracts all pixels into a CSV file for further
    analysis.

    This produces a CSV file where each row corresponds to a pixel. The columns are as follows:
        Column 1: Class labels from the shapefile field labelled as 'attribute'.
        Column 2+ : Band values from the raster at in_ras_path.

    Parameters
    ----------
    in_ras_path : str
        The path to the raster used for creating the training dataset
    training_shape_path : str
        The path to the shapefile containing classification polygons
    out_path : str
        The path for the new .csv file
    attribute : str, optional.
        The label of the field in the training shapefile that contains the classification labels. Defaults to "CODE"

    """
    this_training_data, this_classes = get_training_data(in_ras_path, training_shape_path, attribute=attribute)
    sigs = np.vstack((this_classes, this_training_data.T))
    with open(out_path, 'w', newline='') as outfile:
        writer = csv.writer(outfile)
        writer.writerows(sigs.T)

def create_trained_model(training_image_file_paths, cross_val_repeats = 5, attribute="CODE"):
    """
    Creates a trained model from a set of training images with associated shapefiles.

    This assumes that each image in training_image_file_paths has in the same directory a folder of the same
    name containing a shapefile of the same name. For example, in the folder training_data:

    training_data

      - area1.tif
      - area1

        - area1.shp
        - area1.dbx

       ... rest of shapefile for area 1 ...

      - area2.tif
      - area2

        - area2.shp
        - area2.dbx

       ... rest of shapefile for area 2 ...

    Parameters
    ----------
    training_image_file_paths : list of str
        A list of filepaths to training images.
    cross_val_repeats : int, optional
        The number of cross-validation repeats to use. Defaults to 5.
    attribute : str, optional.
        The label of the field in the training shapefiles that contains the classification labels. Defaults to CODE.

    Returns
    -------
    model : sklearn.classifier
        A fitted scikit-learn model. See notes.
    scores : tuple of floats
        The cross-validation scores for model

    Notes
    ----
    For full details of how to create an appropriate shapefile, see [here](../index.html#training_data).
    At present, the model is an ExtraTreesClassifier arrived at by tpot:
    
    .. code:: python

        model = ens.ExtraTreesClassifier(bootstrap=False, criterion="gini", max_features=0.55,
            min_samples_leaf=2, min_samples_split=16, n_estimators=100, n_jobs=4, class_weight='balanced')

    """
    #TODO: This could be optimised by pre-allocating the training array.
    learning_data = None
    classes = None
    log.info("Collecting training data from all tif/shp file pairs.")
    for training_image_file_path in training_image_file_paths:
        #check whether both the tiff file and the shapefile exist
        training_image_folder, training_image_name = os.path.split(training_image_file_path)
        training_image_name = training_image_name[:-4]  # Strip the file extension
        shape_path_name = training_image_name + '.shp'
        # find the full path to the shapefile, this can be in a subdirectory
        shape_paths = [ f.path for f in os.scandir(training_image_folder) \
                        if f.is_file() and os.path.basename(f) == shape_path_name ]
        if len(shape_paths) == 0:
            log.error("{} not found.".format(shape_path_name))
            continue
        if len(shape_paths) > 1:
            log.warning("Several versions of {} exist. Using the first of these files.".format(shape_path_name))
            for f in shape_paths:
                log.info("  {}".format(f))
        shape_path = shape_paths[0]
        this_training_data, this_classes = get_training_data(training_image_file_path, shape_path, attribute)
        if learning_data is None:
            learning_data = this_training_data
            classes = this_classes
        else:
            learning_data = np.append(learning_data, this_training_data, 0)
            classes = np.append(classes, this_classes)
    log.info("Training the random forest model.")
    log.info("  Class labels: {}".format(np.unique(classes)))
    log.info("  Learning data labels: {}".format(np.unique(learning_data)))
    #TODO: consider training a straight random forest model here
    model = ens.ExtraTreesClassifier(bootstrap=False, criterion="gini", max_features=0.55, min_samples_leaf=2,
                                     min_samples_split=16, n_estimators=100, n_jobs=4, class_weight='balanced')
    model.fit(learning_data, classes)
    scores = cross_val_score(model, learning_data, classes, cv=cross_val_repeats)
    return model, scores


def create_model_for_region(path_to_region, model_out, scores_out, attribute="CODE"):
    """
    Takes all .tif files in a given folder and creates a pickled scikit-learn model for classifying them.
    Wraps :py:func:`create_trained_model`; see docs for that for the details.

    Parameters
    ----------
    path_to_region : str
        Path to the folder containing the tifs.
    model_out : str
        Path to location to save the .pkl file
    scores_out : str
        Path to save the cross-validation scores
    attribute : str, optional
        The label of the field in the training shapefiles that contains the classification labels. Defaults to "CODE".

    """
    log.info("Create model for region based on tif/shp file pairs: {}".format(path_to_region))
    image_glob = os.path.join(path_to_region, r"*.tif")
    image_list = glob.glob(image_glob)
    model, scores = create_trained_model(image_list, attribute=attribute)
    joblib.dump(model, model_out)
    with open(scores_out, 'w') as score_file:
        score_file.write(str(scores))


def create_model_from_signatures(sig_csv_path, model_out, sig_datatype=np.int32):
    """
    Takes a .csv file containing class signatures - produced by extract_features_to_csv - and uses it to train
    and pickle a scikit-learn model.

    Parameters
    ----------
    sig_csv_path : str
        The path to the signatures file
    model_out : str
        The location to save the pickled model to.
    sig_datatype : dtype, optional
        The datatype to read the csv as. Defaults to int32.

    Notes
    -----
    At present, the model is an ExtraTreesClassifier arrived at by tpot:

    .. code:: python
    
        model = ens.ExtraTreesClassifier(bootstrap=False, criterion="gini", max_features=0.55, min_samples_leaf=2,
              min_samples_split=16, n_estimators=100, n_jobs=4, class_weight='balanced')


    """
    model = ens.ExtraTreesClassifier(bootstrap=False, criterion="gini", max_features=0.55, min_samples_leaf=2,
                                     min_samples_split=16, n_estimators=100, n_jobs=4, class_weight='balanced')
    features, labels = load_signatures(sig_csv_path, sig_datatype)
    model.fit(features, labels)
    joblib.dump(model, model_out)


def load_signatures(sig_csv_path, sig_datatype=np.int32):
    """
    Extracts features and class labels from a signature CSV
    Parameters
    ----------
    sig_csv_path : str
        The path to the csv
    sig_datatype : dtype, optional
        The type of pixel data in the signature CSV. Defaults to np.int32

    Returns
    -------
    features : array_like
        a numpy array of the shape (feature_count, sample_count)
    class_labels : array_like of int
        a 1d numpy array of class labels corresponding to the samples in features.

    """
    data = np.genfromtxt(sig_csv_path, delimiter=",", dtype=sig_datatype).T
    return (data[1:, :].T, data[0, :])


def get_training_data(image_path, shape_path, attribute="CODE", shape_projection_id=4326):
    """
    Given an image and a shapefile with categories, returns training data and features suitable
    for fitting a scikit-learn classifier.

    For full details of how to create an appropriate shapefile, see [here](../index.html#training_data).

    Parameters
    ----------
    image_path : str
        The path to the raster image to extract signatures from
    shape_path : str
        The path to the shapefile containing labelled class polygons
    attribute : str, optional
        The shapefile field containing the class labels. Defaults to "CODE".
    shape_projection_id : int, optional
        The EPSG number of the projection of the shapefile. Defaults to EPSG 4326.

    Returns
    -------
    training_data : array_like
        A numpy array of shape (n_pixels, bands), where n_pixels is the number of pixels covered by the training polygons
    features : array_like
        A 1-d numpy array of length (n_pixels) containing the class labels for the corresponding pixel in training_data

    Notes
    -----
    For performance, this uses scikit's sparse.nonzero() function to get the location of each training data pixel.
    This means that this will ignore any classes with a label of '0'.

    """
    # TODO: WRITE A TEST FOR THIS TOO; if this goes wrong, it'll go wrong
    # quietly and in a way that'll cause the most issues further on down the line
    if not os.path.exists(image_path):
        log.error("{} not found.".format(image_path))
        sys.exit(1) 
    if not os.path.exists(shape_path):
        log.error("{} not found.".format(shape_path))
        sys.exit(1) 
    log.info("Get training data from {}".format(image_path))
    log.info("                   and {}".format(shape_path))
    FILL_VALUE = -9999
    with TemporaryDirectory() as td:
        shape_raster_path = os.path.join(td, os.path.basename(shape_path)[:-4]+"_rasterised")
        log.info("Shape raster path {}".format(shape_raster_path))
        shape_raster_path = shapefile_to_raster(shape_path, image_path, shape_raster_path, verbose=False, attribute=attribute, nodata=0)
        image = gdal.Open(image_path)
        rasterised_shapefile = gdal.Open(shape_raster_path)
        shape_array = rasterised_shapefile.GetVirtualMemArray()
        shape_sparse = sp.coo_matrix(np.asarray(shape_array).squeeze())
        y, x, features = sp.find(shape_sparse)
        log.info("{} bands in image file".format(image.RasterCount))
        log.info("{} features in shapefile".format(len(features)))
        log.info("Image raster x size: {}".format(image.RasterXSize))
        log.info("Image raster y size: {}".format(image.RasterYSize))
        log.info("Shape raster x size: {}".format(rasterised_shapefile.RasterXSize))
        log.info("Shape raster y size: {}".format(rasterised_shapefile.RasterYSize))
        training_data = np.empty((len(features), image.RasterCount))
        image_array = image.GetVirtualMemArray()
        image_view = image_array[:,
                    0 : rasterised_shapefile.RasterYSize,
                    0 : rasterised_shapefile.RasterXSize
                    ]
        for index in range(len(features)):
            training_data[index, :] = image_view[:, y[index], x[index]]
        image_view = None
        image_array = None
        shape_array = None
        image = None
        rasterised_shapefile = None
        return training_data, features


def raster_reclass_binary(img_path, rcl_value, outFn, outFmt='GTiff', write_out=True):
    """
    Takes a raster and reclassifies rcl_value to 1, with all others becoming 0. In-place operation if write_out is True.

    Parameters
    ----------
    img_path : str
        Path to 1 band input  raster.
    rcl_value : int
        Integer indication the value that should be reclassified to 1. All other values will be 0.
    outFn : str
        Output file name.
    outFmt : str, optional
        Output format. Set to GTiff by default. Other GDAL options available.
    write_out : bool, optional.
        Set to True by default. Will write raster to disk. If False, only an array is returned

    Returns
    -------
    Reclassifies numpy array
    """
    log = logging.getLogger(__name__)
    log.info('Starting raster reclassification.')
    # load in classification raster
    in_ds = gdal.Open(img_path)
    in_band = in_ds.GetRasterBand(1)
    in_array = in_band.ReadAsArray()

    # reclassify
    in_array[in_array != rcl_value] = 0
    in_array[in_array == rcl_value] = 1

    if write_out:
        driver = gdal.GetDriverByName(str(outFmt))
        out_ds = driver.Create(outFn, in_band.XSize, in_band.YSize, 1,
                               in_band.DataType)
        out_ds.SetProjection(in_ds.GetProjection())
        out_ds.SetGeoTransform(in_ds.GetGeoTransform())
        # Todo: Check for existing files. Skip if exists or make overwrite optional.
        out_ds.GetRasterBand(1).WriteArray(in_array)

        # write the data to disk
        out_ds.FlushCache()

        # Compute statistics on each output band
        # setting ComputeStatistics to false calculates stats on all pixels not estimates
        out_ds.GetRasterBand(1).ComputeStatistics(False)

        out_ds.BuildOverviews("average", [2, 4, 8, 16, 32])

        out_ds = None

    return in_array


def shapefile_to_raster(shapefilename, inraster_filename, outraster_filename, verbose=False, attribute="Class", nodata=0):
    '''
    Reads in a shapefile with polygons and produces a raster file that 
    aligns with an input rasterfile (same corner coordinates, resolution, coordinate 
    reference system and geotransform). Each pixel value in the output raster will
    indicate the number from the shapefile based on the selected attribute column.
    Based on https://gis.stackexchange.com/questions/151339/rasterize-a-shapefile-with-geopandas-or-fiona-python  

    Parameters
    ----------
    shapefilename : str
      String pointing to the input shapefile in ESRI format.

    inraster_filename : str
      String pointing to the input raster file that we want to align the output raster to.

    outraster_filename : str
      String pointing to the output raster file.

    verbose : boolean
      True or False. If True, additional text output will be printed to the log file.

    attribute : str
      Name of the column of the attribute table of the shapefile that will be burned into the raster.

    nodata : int
      No data value.


    Returns:
    ----------
    outraster_filename : str
    '''

    log.info("Shapefile to Raster:")
    log.info("  shapefile name {}".format(shapefilename))
    log.info("  inrasterfile name {}".format(inraster_filename))
    log.info("  outrasterfile name {}".format(outraster_filename))
    with TemporaryDirectory() as td:
        image = gdal.Open(inraster_filename)
        image_gt = image.GetGeoTransform()
        drv = ogr.GetDriverByName("ESRI Shapefile")
        if drv is None:
            log.error("  {} driver not available.".format("ESRI Shapefile"))
            sys.exit(1)
        inshape = drv.Open(shapefilename)
        inlayer = inshape.GetLayer()
        out_path = os.path.join(td, os.path.basename(outraster_filename))
        x_min = image_gt[0]
        y_max = image_gt[3]
        x_max = x_min + image_gt[1] * image.RasterXSize
        y_min = y_max + image_gt[5] * image.RasterYSize
        x_res = image.RasterXSize
        y_res = image.RasterYSize
        pixel_width = image_gt[1]
        target_ds = gdal.GetDriverByName('GTiff').Create(out_path, x_res, y_res, 1, gdal.GDT_Int16)
        target_ds.SetGeoTransform((x_min, pixel_width, 0, y_min, 0, pixel_width))
        band = target_ds.GetRasterBand(1)
        band.SetNoDataValue(nodata)
        band.FlushCache()
        gdal.RasterizeLayer(target_ds, [1], inlayer, options=["ATTRIBUTE=CLASS"]) #.format(attribute)])
        log.info("{} exists? {}".format(out_path, os.path.exists(out_path)))
        log.info("{} exists? {}".format(os.path.dirname(shapefilename), os.path.exists(os.path.dirname(shapefilename))))
        shutil.move(out_path, outraster_filename)
        target_ds = None
        image = None
        inshape = None
        inlayer = None
    return outraster_filename

def train_rf_model(raster, samples, modelfile, ntrees = 101, weights = None):
    '''
    Trains a random forest classifier model based on a raster file with bands
      as features and a second raster file with training data, in which pixel
      values indicate the class.

    Args:
      raster = filename and path to the raster file to be classified in tiff format
      samples = filename and path to the raster file with the training samples 
        as pixel values (in tiff format)
      modelfile = filename and path to a pickle file to save the trained model to
      ntrees (optional) = number of trees in the random forest, default = 101
      weights (optional) = a list of integers giving weights for all classes. 
        If not specified, all weights will be equal.
    
    Returns:
      random forest model object
    '''

    # read in raster from geotiff
    img_ds = io.imread(raster)

    # convert to 16bit numpy array 
    img = np.array(img_ds, dtype='int16')

    # read in the training sample pixels 
    roi_ds = io.imread(samples)   
    roi = np.array(roi_ds, dtype='int8')  
    
    # read in the class labels
    labels = np.unique(roi[roi > 0]) 
    nclasses = labels.size # number of unique class values
    print('The training data include {n} classes: {classes}'.format(n=nclasses, classes=labels))

    # compose the X,Y pixel positions (feature dataset and training dataset)
    # 0 = missing class value
    X = img[roi > 0, :] 
    Y = roi[roi > 0]     

    # create a dictionary of class weights (class 1 has the weight 1, etc.)
    w = dict() # create an empty dictionary
    for i in range(nclasses): # iterate over all classes from 0 to nclasses-1
      if weights == None:
        w[i+1] = '1' # if not specified, set all weights to 1  
      else:
        if weights.size >= nclasses: # if enough weights are given, assign them
          w[i+1] = weights[i] # assign the weights if specified by the user
        else: # if fewer weights are defined than the number of classes, then set the remaining weights to 1
          if i > weights.size:
            w[i+1] = '1' # set weight to 1
          else:
            w[i+1] = weights[i] # assign the weights if specified by the user

    # build the Random Forest Classifier 
    # for more information: http://scikit-learn.org/stable/modules/generated/sklearn.ensemble.RandomForestClassifier.html

    rf = RandomForestClassifier(class_weight = weights, n_estimators = ntrees, criterion = 'gini', max_depth = 4, 
                                min_samples_split = 2, min_samples_leaf = 1, max_features = 'auto', 
                                bootstrap = True, oob_score = True, n_jobs = 1, random_state = None, verbose = True)  

    # fit the model to the training data and the feature dataset
    rf = rf.fit(X,Y)

    # export the Random Forest model to a file
    joblib.dump(rf, modelfile)
    
    # calculate the feature importances
    importances = rf.feature_importances_
    std = np.std([tree.feature_importances_ for tree in rf.estimators_], axis=0)
    indices = np.argsort(importances)[::-1]

    # Print the feature ranking
    print("Feature ranking:")
    for f in range(X.shape[1]):
        print("%d. feature %d (%f)" % (f + 1, indices[f], importances[indices[f]]))

    # Plot the feature importances of the forest
    plt.figure()
    plt.title("Feature importances")
    plt.bar(range(X.shape[1]), importances[indices], color="r", yerr=std[indices], align="center")
    plt.xticks(range(X.shape[1]), indices)
    plt.xlim([-1, X.shape[1]])
    plt.show()
    
    # Out-of-bag error rate as a function of number of trees:
    oob_error = [] # define an empty list with pairs of values
    
    # Range of `n_estimators` values to explore.
    mintrees = 30 # this needs to be a sensible minimum number to get reliable OOB error estimates
    maxtrees = max(mintrees, ntrees) # go all the way to the highest number of trees
    nsteps = 5 # number of steps to calculate OOB error rate for (saves time)
    
    # work out the error rate for each number of trees in the random forest
    for i in range(mintrees, maxtrees + 1, round((maxtrees - mintrees)/nsteps)): # start, end, step
      rf.set_params(n_estimators=i)
      rf.fit(X, Y)
      oob_error.append((i, 1 - rf.oob_score_))

    # Plot OOB error rate vs. number of trees
    xs, ys = zip(*oob_error)
    plt.plot(xs, ys)
    # plt.xlim(0, maxtrees)
    plt.xlabel("n_estimators")
    plt.ylabel("OOB error rate")
    # plt.legend(loc="upper right")
    plt.show()

    return(rf) # returns the random forest model object

def classify_rf(raster, modelfile, outfile, verbose = False):
  '''
  Reads in a pickle file of a random forest model and a raster file with feature layers,
    and classifies the raster file using the model.

  Args:
    raster = filename and path to the raster file to be classified (in tiff uint16 format)
    modelfile = filename and path to the pickled file with the random forest model in uint8 format
    outfile = filename and path to the output file with the classified map in uint8 format
    verbose (optional) = True or False. If True, provides additional printed output.
  '''

  # Read Data    
  src = rasterio.open(raster, 'r')   
  img = src.read()

  if verbose:
    print("img.shape = ", img.shape)

  # get number of bands
  n = img.shape[0]

  if verbose:
    print(n, " Bands")

  # load your random forest model from the pickle file
  clf = joblib.load(modelfile)    

  # to work with SciKitLearn, we have to reshape the raster as an image
  # this will change the shape from (bands, rows, columns) to (rows, columns, bands)
  img = reshape_as_image(img)

  # next, we have to reshape the image again into (rows * columns, bands)
  # because that is what SciKitLearn asks for
  new_shape = (img.shape[0] * img.shape[1], img.shape[2]) 

  if verbose:
    print("img[:, :, :n].shape = ", img[:, :, :n].shape)
    print("new_shape = ", new_shape)

  img_as_array = img[:, :, :n].reshape(new_shape)   

  if verbose:
    print("img_as_array.shape = ", img_as_array.shape)

  # classify it
  class_prediction = clf.predict(img_as_array) 

  # and reshape the flattened array back to its original dimensions
  if verbose:
    print("class_prediction.shape = ", class_prediction.shape)
    print("img[:, :, 0].shape = ", img[:, :, 0].shape)

  class_prediction = np.uint8(class_prediction.reshape(img[:, :, 0].shape))

  if verbose:
    print(class_prediction.dtype)
  
  # save the image as a uint8 Geotiff file
  tmpfile = rasterio.open(outfile, 'w', driver='Gtiff', 
                          width=src.width, height=src.height,
                          count=1, crs=src.crs, transform=src.transform, 
                          dtype=np.uint8)

  tmpfile.write(class_prediction, 1)

  tmpfile.close()

