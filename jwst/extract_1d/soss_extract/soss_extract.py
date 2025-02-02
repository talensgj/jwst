import logging

import numpy as np
from scipy.interpolate import UnivariateSpline

from stdatamodels import DataModel

from ... import datamodels
from ...datamodels.dqflags import pixel
from astropy.nddata.bitmask import bitfield_to_boolean_mask

from .soss_syscor import make_background_mask, soss_background
from .soss_solver import solve_transform, transform_wavemap, transform_profile, transform_coords
from .atoca import ExtractionEngine
from .atoca_utils import ThroughputSOSS, WebbKernel, grid_from_map, mask_bad_dispersion_direction
from .soss_boxextract import get_box_weights, box_extract, estim_error_nearest_data

# TODO remove once code is sufficiently tested.
from . import devtools

log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)


def get_ref_file_args(ref_files, transform):
    """Prepare the reference files for the extraction engine.

    :param ref_files: A dictionary of the reference file DataModels. # TODO not final?
    :param transform: A 3-elemnt list or array describing the rotation and
        translation to apply to the reference files in order to match the
        observation.

    :type ref_files: dict
    :type transform: array_like

    :returns: The reference file args used with the extraction engine.
    :rtype: Tuple(wavemaps, specprofiles, throughputs, kernels)
    """

    # The wavelength maps for order 1 and 2.
    wavemap_ref = ref_files['wavemap']

    ovs = wavemap_ref.map[0].oversampling
    pad = wavemap_ref.map[0].padding

    wavemap_o1 = transform_wavemap(transform, wavemap_ref.map[0].data, ovs, pad)
    wavemap_o2 = transform_wavemap(transform, wavemap_ref.map[1].data, ovs, pad)

    # Make sure all pixels follow the expected direction of the dispersion
    # Here, the dispersion axis is given by the columns (dispersion_axis=1)
    # and it is decreasing going from left to right (dwv_sign=-1)
    kwargs = dict(dispersion_axis=1, dwv_sign=-1)
    wavemap_o1, flag_o1 = mask_bad_dispersion_direction(wavemap_o1, **kwargs)
    wavemap_o2, flag_o2 = mask_bad_dispersion_direction(wavemap_o2, **kwargs)

    # Warn if not all pixels were corrected
    msg_warning = 'Some pixels in order {} do not follow the expected dispersion axis'
    if not flag_o1:
        log.warning(msg_warning.format(1))
    if not flag_o2:
        log.warning(msg_warning.format(2))

    # The spectral profiles for order 1 and 2.
    specprofile_ref = ref_files['specprofile']
    ovs = specprofile_ref.profile[0].oversampling
    pad = specprofile_ref.profile[0].padding

    # TODO unclear if norm should be True or False.
    specprofile_o1 = transform_profile(transform, specprofile_ref.profile[0].data, ovs, pad, norm=False)
    specprofile_o2 = transform_profile(transform, specprofile_ref.profile[1].data, ovs, pad, norm=False)

    # The throughput curves for order 1 and 2.
    spectrace_ref = ref_files['spectrace']

    throughput_o1 = ThroughputSOSS(spectrace_ref.trace[0].data['WAVELENGTH'], spectrace_ref.trace[0].data['THROUGHPUT'])
    throughput_o2 = ThroughputSOSS(spectrace_ref.trace[1].data['WAVELENGTH'], spectrace_ref.trace[1].data['THROUGHPUT'])

    # The spectral kernels.
    speckernel_ref = ref_files['speckernel']
    ovs = speckernel_ref.meta.spectral_oversampling
    n_pix = 2*speckernel_ref.meta.halfwidth + 1

    # Take the centroid of each trace as a grid to project the WebbKernel
    # WebbKer needs a 2d input, so artificially add axis
    wave_maps = [wavemap_o1, wavemap_o2]
    centroid = dict()
    for wv_map, order in zip(wave_maps, [1, 2]):
        # Needs the same number of columns as the detector. Put zeros where not define.
        wv_cent = np.zeros((1, wv_map.shape[1]))
        # Get central wavelength as a function of columns
        col, _, wv = get_trace_1d(ref_files, transform, order)
        wv_cent[:, col] = wv
        # Set invalid values to zero
        idx_invalid = ~np.isfinite(wv_cent)
        wv_cent[idx_invalid] = 0.0
        centroid[order] = wv_cent

    # Get kernels
    kernels_o1 = WebbKernel(speckernel_ref.wavelengths, speckernel_ref.kernels, centroid[1], ovs, n_pix)
    kernels_o2 = WebbKernel(speckernel_ref.wavelengths, speckernel_ref.kernels, centroid[2], ovs, n_pix)

    # TODO This temporary fix may be removed eventually or put somewhere else, or deal with in WebbKernel?
    # Temporary fix to make sure that the kernels can cover the wavelength maps
    speckernel_wv_range = [np.min(speckernel_ref.wavelengths), np.max(speckernel_ref.wavelengths)]
    valid_wavemap = (speckernel_wv_range[0] <= wavemap_o1) & (wavemap_o1 <= speckernel_wv_range[1])
    wavemap_o1 = np.where(valid_wavemap, wavemap_o1, 0.)
    valid_wavemap = (speckernel_wv_range[0] <= wavemap_o2) & (wavemap_o2 <= speckernel_wv_range[1])
    wavemap_o2 = np.where(valid_wavemap, wavemap_o2, 0.)

    return [wavemap_o1, wavemap_o2], [specprofile_o1, specprofile_o2], [throughput_o1, throughput_o2], [kernels_o1, kernels_o2]


def get_trace_1d(ref_files, transform, order, cols=None):
    """Get the x, y, wavelength of the trace after applying the transform.

    :param ref_files: A dictionary of the reference file DataModels. # TODO not final?
    :param transform: A 3-elemnt list or array describing the rotation and
        translation to apply to the reference files in order to match the
        observation.
    :param order: The spectral order for which to return the trace parameters.
    :param cols: The columns on the detector for which to compute the trace
        parameters. # TODO not sure the cols argument adds usefull functionality, remove?

    :type ref_files: dict
    :type transform: array_like
    :type order: int
    :type cols: array[int]

    :returns: xtrace, ytrace, wavetrace - The x, y and wavelength of the trace.
    :rtype: Tuple(array[float], array[float], array[float])
    """

    if cols is None:
        xtrace = np.arange(2048)
    else:
        xtrace = cols

    spectrace_ref = ref_files['spectrace']

    # Read x, y, wavelength for the relevant order.
    xref = spectrace_ref.trace[order - 1].data['X']
    yref = spectrace_ref.trace[order - 1].data['Y']
    waveref = spectrace_ref.trace[order - 1].data['WAVELENGTH']

    # Rotate and shift the positions based on transform.
    angle, xshift, yshift = transform
    xrot, yrot = transform_coords(angle, xshift, yshift, xref, yref)

    # Interpolate y and wavelength to the requested columns.
    sort = np.argsort(xrot)
    ytrace = np.interp(xtrace, xrot[sort], yrot[sort])
    wavetrace = np.interp(xtrace, xrot[sort], waveref[sort])

    return xtrace, ytrace, wavetrace


def estim_flux_first_order(scidata_bkg, scierr, scimask, ref_files, threshold):
    """
    Parameters
    ----------
    scidata_bkg: array
        A single background subtracted NIRISS SOSS detector image.
    scierr: array
        The uncertainties corresponding to the detector image.
    scimask: array
        Pixel that should be masked from the detector image.
    ref_files: list
        A list of list of the reference files for each orders.
    threshold: float
        The threshold value for using pixels based on the spectral profile.
    Returns
    -------
    A function to estimate the underlying flux as a function of wavelength
    """

    # Unpack ref_files
    wave_maps, spat_pros, thrpts, _ = ref_files

    # Oversampling of 1 to make sure the solution will be stable
    n_os = 1

    # Define wavelength grid based on order 1 only (so first index)
    wave_grid = grid_from_map(wave_maps[0], spat_pros[0], n_os=n_os)

    # Mask parts contaminated by order 2 based on its spatial profile
    mask = (spat_pros[1] >= threshold) | scimask

    # Init extraction without convolution kernel (so extract the spectrum at order 1 resolution)
    ref_file_args = [wave_maps[0]], [spat_pros[0]], [thrpts[0]], [np.array([1.])]
    kwargs = {'wave_grid': wave_grid,
              'orders': [1],
              'global_mask': mask,
              'threshold': threshold}
    engine = ExtractionEngine(*ref_file_args, **kwargs)

    # Extract estimate
    spec_estimate = engine.extract(data=scidata_bkg, error=scierr)

    # Interpolate
    idx = np.isfinite(spec_estimate)
    estimate_spl = UnivariateSpline(wave_grid[idx], spec_estimate[idx], k=3, s=0, ext=0)

    return estimate_spl


def _mask_wv_map_centroid_outside(wave_maps, ref_files, transform, y_max, orders=(1, 2)):
    """ Patch to mask wv_map when centroid outside"""
    for wv_map, order in zip(wave_maps, orders):
        # Get centroid wavelength and y position as a function of columns
        _, y_pos, wv = get_trace_1d(ref_files, transform, order)
        # Find min and max wavelengths with centroid inside of detector
        wv = wv[np.isfinite(y_pos)]
        y_pos = y_pos[np.isfinite(y_pos)]
        idx_inside = (0 <= y_pos) & (y_pos <= y_max)
        wv = wv[idx_inside]
        # Set to zeros (mask) values outside
        mask = np.isfinite(wv_map)
        mask[mask] = (np.min(wv) > wv_map[mask]) | (wv_map[mask] > np.max(wv))
        wv_map[mask] = 0.


def model_image(scidata_bkg, scierr, scimask, refmask, ref_file_args, transform=None,
                tikfac=None, n_os=5, threshold=1e-4, devname=None, soss_filter='CLEAR'):
    """Perform the spectral extraction on a single image.

    :param scidata_bkg: A single background subtracted NIRISS SOSS detector image.
    :param scierr: The uncertainties corresponding to the detector image.
    :param scimask: Pixel that should be masked from the detector image.
    :param refmask: Pixels that should never be reconstructed e.g. the reference pixels.
    :param ref_files: A dictionary of the reference file DataModels. # TODO not final?
    :param transform: A 3-elemnt list or array describing the rotation and
        translation to apply to the reference files in order to match the
        observation. If None the transformation is computed.
    :param tikfac: The Tikhonov regularization factor used when solving for
        the uncontaminated flux.
    :param n_os: The oversampling factor of the wavelength grid used when
        solving for the uncontaminated flux.
    :param threshold: The threshold value for using pixels based on the spectral
        profile.

    :type scidata_bkg: array[float]
    :type scierr: array[float]
    :type scimask: array[bool]
    :type refmask: array[bool]
    :type ref_files: dict
    :type transform: array_like
    :type tikfac: float
    :type n_os: int
    :type threshold: float

    :returns: TODO TBD
    :rtype: TODO TBD
    """

    # Some error values are 0, we need to mask those pixels for the extraction engine.
    scimask = scimask | ~(scierr > 0)

    log.info('Extracting using transformation parameters {}'.format(transform))

    # TODO This is a temporary fix until the wave_grid is specified directly in model_image
    # Make sure wavelength maps cover only parts where the centroid is inside the detector image
    #_mask_wv_map_centroid_outside(ref_file_args[0], ref_files, transform, scidata_bkg.shape[0])

    # Set the c_kwargs using the minimum value of the kernels
    c_kwargs = [{'thresh': webb_ker.min_value} for webb_ker in ref_file_args[3]]

    # Initialize the Engine.
    engine = ExtractionEngine(*ref_file_args, n_os=n_os, threshold=threshold, c_kwargs=c_kwargs)

    if tikfac is None:

        log.info('Solving for the optimal Tikhonov factor.')

        # Need a rough estimate of the underlying flux to estimate the tikhonov factor
        estimate = estim_flux_first_order(scidata_bkg, scierr, scimask, ref_file_args, threshold)

        # Find the tikhonov factor.
        # Initial pass 8 orders of magnitude with 10 grid points.
        factors = engine.estimate_tikho_factors(estimate, log_range=[-4, 4], n_points=10)
        tiktests = engine.get_tikho_tests(factors, data=scidata_bkg, error=scierr, mask=scimask)
        tikfac, mode, _ = engine.best_tikho_factor(tests=tiktests, fit_mode='chi2')

        # Refine across 4 orders of magnitude.
        tikfac = np.log10(tikfac)
        factors = np.logspace(tikfac - 2, tikfac + 2, 20)
        tiktests = engine.get_tikho_tests(factors, data=scidata_bkg, error=scierr, mask=scimask)
        tikfac, mode, _ = engine.best_tikho_factor(tests=tiktests, fit_mode=mode)

    log.info('Using a Tikhonov factor of {}'.format(tikfac))

    # Run the extract method of the Engine.
    f_k = engine.extract(data=scidata_bkg, error=scierr, mask=scimask, tikhonov=True, factor=tikfac)

    # Compute the log-likelihood of the best fit.
    logl = engine.compute_likelihood(f_k, same=False)

    log.info('Optimal solution has a log-likelihood of {}'.format(logl))

    # Create a new instance of the engine for evaluating the trace model.
    # This allows bad pixels and pixels below the threshold to be reconstructed as well.
    # TODO with the right parameters could we rebuild order 3 as well?
    # Model the order 1 and order 2 trace seperately.
    order_list = ['Order 1', 'Order 2']
    tracemodels = dict()
    for i_order, order in enumerate(order_list):

        log.info('Building the model image of the {}.'.format(order))

        # Take only the order's specific ref_files
        ref_file_order = [[ref_f[i_order]] for ref_f in ref_file_args]

        # Pre-convolve the extracted flux (f_k) at the order's resolution
        # so that the convolution matrix must not be re-computed.
        flux_order = engine.kernels[i_order].dot(f_k)

        # Then must take the grid after convolution (smaller)
        grid_order = engine.wave_grid_c(i_order)

        # Keep only valid values to make sure there will be no Nans in the order model
        idx_valid = np.isfinite(flux_order)
        grid_order, flux_order = grid_order[idx_valid], flux_order[idx_valid]

        # And give the identity kernel to the Engine (so no convolution)
        ref_file_order[3] = [np.array([1.])]

        # Build model of the order
        model_kwargs = {'wave_grid': grid_order,
                        'threshold': 1e-5,
                        'global_mask': refmask,
                        'orders': [i_order + 1]}
        model = ExtractionEngine(*ref_file_order, **model_kwargs)

        # Project on detector and save in dictionary
        tracemodels[order] = model.rebuild(flux_order)

    # TODO temporary debug plot.
    if devname is not None:
        dev_tools_args = (scidata_bkg, scierr, scimask)
        dev_tools_args += (tracemodels['Order 1'], tracemodels['Order 2'])
        devtools.diagnostic_plot(*dev_tools_args, devname=devname)

    return tracemodels, tikfac, logl


def extract_image(scidata_bkg, scierr, scimask, tracemodels, ref_files,
                  transform, subarray, width=40., bad_pix='model', devname=None):
    """Perform the box-extraction on the image, while using the trace model to
    correct for contamination.
    Parameters
    ----------
    scidata_bkg: array[float]
        A single backround subtracted NIRISS SOSS detector image.
    scierr: array[float]
        The uncertainties corresponding to the detector image.
    scimask: array[float]
        Pixel that should be masked from the detector image.
    tracemodels: dict
        Dictionary of the modeled detector images for each orders.
    ref_files: dict
        A dictionary of the reference file DataModels. # TODO not final?
    transform: array_like
        A 3-elemnt list or array describing the rotation and
        translation to apply to the reference files in order to match the
        observation. If None the transformation is computed.
    subarray: str
        'SUBSTRIPT96' or 'SUBSTRIP256' or 'FULL'
    width: float
        The width of the aperture used to extract the un-contaminated spectrum.
    bad_pix: str
        How to handle the bad pixels. Options are 'masking' and 'model'.
        'masking' will simply mask the bad pixels, so the number of pixels in each columns
        in the box extraction will not be constant.
        'model' option uses `tracemodels` to replace the bad pixels.
    Returns
    -------
    wavelengths, fluxes, fluxerrs, npixels, box_weights
    Each is a dictionary with each extracted orders as key.
    """
    # Which orders to extract.
    if subarray == 'SUBSTRIP96':
        order_list = [1]
    else:
        order_list = [1, 2, 3]

    order_str = {order: f'Order {order}' for order in order_list}

    # List of modeled orders
    mod_order_list = tracemodels.keys()

    # Create dictionaries for the output spectra.
    wavelengths = dict()
    fluxes = dict()
    fluxerrs = dict()
    npixels = dict()
    box_weights = dict()

    log.info('Performing the de-contaminated box-extraction.')

    # Extract each order from order list
    for order_integer in order_list:
        # Order string-name is used more often than integer-name
        order = order_str[order_integer]

        log.debug(f'Extracting {order}.')

        # Define the box aperture
        xtrace, ytrace, wavelengths[order] = get_trace_1d(ref_files, transform, order_integer)
        box_w_ord = get_box_weights(ytrace, width, scidata_bkg.shape, cols=xtrace)

        # Decontaminate using all other modeled orders
        decont = scidata_bkg
        for mod_order in mod_order_list:
            if mod_order != order:
                log.debug(f'Decontaminating {order} from {mod_order} using model.')
                decont = decont - tracemodels[mod_order]

        # TODO Add the option 'interpolate' to bad pixel handling.
        # Deal with bad pixels if required.
        if bad_pix == 'model':
            # Model the bad pixels decontaminated image when available
            try:
                # Replace bad pixels
                decont = np.where(scimask, tracemodels[order], decont)
                # Update the mask for the modeled order, so all the pixels are usable.
                scimask_ord = np.zeros_like(scimask)

                log.debug(f'Bad pixels in {order} are replaced with trace model.')

                # Replace error estimate of the bad pixels using other valid pixels of similar value.
                # The pixel to be estimate are the masked pixels in the region of extraction
                extraction_region = (box_w_ord > 0)
                pix_to_estim = (extraction_region & scimask)
                # Use only valid pixels (not masked) in the extraction region for the empirical estimation
                valid_pix = (extraction_region & ~scimask)
                scierr_ord = estim_error_nearest_data(scierr, decont, pix_to_estim, valid_pix)

            except KeyError:
                # Keep same mask and error
                scimask_ord = scimask
                scierr_ord = scierr
                log.warning(f'Bad pixels in {order} will be masked instead of modeled: trace model unavailable.')
        else:
            # Mask pixels
            scimask_ord = scimask
            log.debug(f'Bad pixels in {order} will be masked.')

        # Save box weights
        box_weights[order] = box_w_ord
        # Perform the box extraction and save
        out = box_extract(decont, scierr_ord, scimask_ord, box_w_ord, cols=xtrace)
        _, fluxes[order], fluxerrs[order], npixels[order] = out

    # TODO temporary debug plot.
    if devname is not None:
        devtools.plot_1d_spectra(wavelengths, fluxes, fluxerrs, npixels, devname)

    return wavelengths, fluxes, fluxerrs, npixels, box_weights


def run_extract1d(input_model: DataModel,
                  spectrace_ref_name: str,
                  wavemap_ref_name: str,
                  specprofile_ref_name: str,
                  speckernel_ref_name: str,
                  subarray: str,
                  soss_filter: str,
                  soss_kwargs: dict):
    """Run the spectral extraction on NIRISS SOSS data.

    :param input_model:
    :param spectrace_ref_name:
    :param wavemap_ref_name:
    :param specprofile_ref_name:
    :param speckernel_ref_name:
    :param subarray:
    :param soss_filter:
    :param soss_kwargs:

    :type input_model:
    :type spectrace_ref_name:
    :type wavemap_ref_name:
    :type specprofile_ref_name:
    :type speckernel_ref_name:
    :type subarray:
    :type soss_filter:
    :type soss_kwargs:

    :returns: An output_model containing the extracted spectra.
    :rtype:
    """
    # Map the order integer names to the string names
    # TODO Could be better to simply use the integer name everywhere (1 instead of 'Order 1')
    order_str_2_int = {f'Order {order}': order for order in [1, 2, 3]}

    # Read the reference files.
    spectrace_ref = datamodels.SpecTraceModel(spectrace_ref_name)
    wavemap_ref = datamodels.WaveMapModel(wavemap_ref_name)
    specprofile_ref = datamodels.SpecProfileModel(specprofile_ref_name)
    speckernel_ref = datamodels.SpecKernelModel(speckernel_ref_name)

    ref_files = dict()
    ref_files['spectrace'] = spectrace_ref
    ref_files['wavemap'] = wavemap_ref
    ref_files['specprofile'] = specprofile_ref
    ref_files['speckernel'] = speckernel_ref

    # Initialize the output model and output references (model of the detector and box aperture weights).
    output_model = datamodels.MultiSpecModel()  # TODO is this correct for CubeModel input?
    output_model.update(input_model)  # Copy meta data from input to output.

    output_references = datamodels.SossExtractModel()
    output_references.update(input_model)

    all_tracemodels = dict()
    all_box_weights = dict()

    # Extract depending on the type of datamodels (Image or Cube)
    if isinstance(input_model, datamodels.ImageModel):

        log.info('Input is an ImageModel, processing a single integration.')

        # Initialize the theta, dx, dy transform parameters
        transform = soss_kwargs.pop('transform')

        # Received a single 2D image set dtype to float64 and convert DQ to boolian mask.
        scidata = input_model.data.astype('float64')
        scierr = input_model.err.astype('float64')
        scimask = input_model.dq > 0  # Mask bad pixels with True.
        refmask = bitfield_to_boolean_mask(input_model.dq, ignore_flags=pixel['REFERENCE_PIXEL'], flip_bits=True)

        # Perform background correction.
        bkg_mask = make_background_mask(scidata, width=40)
        scidata_bkg, col_bkg, npix_bkg = soss_background(scidata, scimask, bkg_mask=bkg_mask)

        # Determine the theta, dx, dy transform needed to match scidata trace position to ref file position.
        if transform is None:
            log.info('Solving for the transformation parameters.')

            # Unpack the expected order 1 & 2 positions.
            spectrace_ref = ref_files['spectrace']
            xref_o1 = spectrace_ref.trace[0].data['X']
            yref_o1 = spectrace_ref.trace[0].data['Y']
            xref_o2 = spectrace_ref.trace[1].data['X']
            yref_o2 = spectrace_ref.trace[1].data['Y']

            # Use the solver on the background subtracted image.
            if subarray == 'SUBSTRIP96' or soss_filter == 'F277':
                # Use only order 1 to solve theta, dx, dy
                transform = solve_transform(scidata_bkg, scimask, xref_o1, yref_o1, soss_filter=soss_filter)
            else:
                transform = solve_transform(scidata_bkg, scimask, xref_o1, yref_o1,
                                            xref_o2, yref_o2, soss_filter=soss_filter)

        log.info('Measured to Reference trace position transform: theta={:.4f}, dx={:.4f}, dy={:.4f}'.format(
            transform[0], transform[1], transform[2]))

        # Prepare the reference file arguments.
        ref_file_args = get_ref_file_args(ref_files, transform)

        # TODO This is a temporary fix until the wave_grid is specified directly in model_image
        # Make sure wavelength maps cover only parts where the centroid is inside the detector image
        _mask_wv_map_centroid_outside(ref_file_args[0], ref_files, transform, scidata_bkg.shape[0])

        # Model the traces based on optics filter configuration (CLEAR or F277W)
        if soss_filter == 'CLEAR':

            # Model the image.
            kwargs = dict()
            kwargs['transform'] = transform
            kwargs['tikfac'] = soss_kwargs['tikfac']
            kwargs['n_os'] = soss_kwargs['n_os']
            kwargs['threshold'] = soss_kwargs['threshold']
            kwargs['devname'] = soss_kwargs['devname']

            result = model_image(scidata_bkg, scierr, scimask, refmask, ref_file_args, **kwargs)
            tracemodels, soss_kwargs['tikfac'], logl = result

        else:
            # No model can be fit
            # TODO: Implement model fitting for the F277W. Needs a specific F277W throughput ref file
            tracemodels = dict()

        # Save trace models for output reference
        for order in tracemodels:
            # Save as a list (convert to array at the end)
            all_tracemodels[order] = [tracemodels[order]]

        # Use the trace models to perform a de-contaminated extraction.
        kwargs = dict()
        kwargs['width'] = soss_kwargs['width']
        kwargs['devname'] = soss_kwargs['devname']
        kwargs['bad_pix'] = soss_kwargs['bad_pix']

        result = extract_image(scidata_bkg, scierr, scimask, tracemodels, ref_files, transform, subarray, **kwargs)
        wavelengths, fluxes, fluxerrs, npixels, box_weights = result

        # Save box weights for output reference
        for order in box_weights:
            # Save as a list (convert to array at the end)
            all_box_weights[order] = [box_weights[order]]

        # Copy spectral data for each order into the output model.
        # TODO how to include parameters like transform and tikfac in the output.
        for order in wavelengths.keys():

            table_size = len(wavelengths[order])

            out_table = np.zeros(table_size, dtype=datamodels.SpecModel().spec_table.dtype)
            out_table['WAVELENGTH'] = wavelengths[order]
            out_table['FLUX'] = fluxes[order]
            out_table['FLUX_ERROR'] = fluxerrs[order]
            out_table['DQ'] = np.zeros(table_size)  # TODO how should these be set?
            out_table['BACKGROUND'] = col_bkg  # TODO this is a columnwise, per pixel, background value computed and subtracted before the solver, engine and box extraction.
            out_table['NPIXELS'] = npixels[order]

            spec = datamodels.SpecModel(spec_table=out_table)

            # Add integration number and spectral order
            spec.spectral_order = order_str_2_int[order]

            output_model.spec.append(spec)

    elif isinstance(input_model, datamodels.CubeModel):

        nimages = len(input_model.data)  # TODO Do this or use meta.exposure.

        log.info('Input is a CubeModel containing {} integrations.'.format(nimages))

        # Initialize the theta, dx, dy transform parameters
        transform = soss_kwargs.pop('transform')

        # Build deepstack out of max N images TODO OPTIONAL.
        # TODO making a deepstack could be used to get a more robust transform and tikfac, 1/f.

        # Loop over images.
        for i in range(nimages):

            log.info('Processing integration {} of {}.'.format(i + 1, nimages))

            # Unpack the i-th image, set dtype to float64 and convert DQ to boolian mask.
            scidata = input_model.data[i].astype('float64')
            scierr = input_model.err[i].astype('float64')
            scimask = input_model.dq[i] > 0
            refmask = bitfield_to_boolean_mask(input_model.dq[i], ignore_flags=pixel['REFERENCE_PIXEL'], flip_bits=True)

            # Perform background correction.
            bkg_mask = make_background_mask(scidata, width=40)
            scidata_bkg, col_bkg, npix_bkg = soss_background(scidata, scimask, bkg_mask=bkg_mask)

            # Determine the theta, dx, dy transform needed to match scidata trace position to ref file position.
            if transform is None:
                log.info('Solving for the transformation parameters.')

                # Unpack the expected order 1 & 2 positions.
                spectrace_ref = ref_files['spectrace']
                xref_o1 = spectrace_ref.trace[0].data['X']
                yref_o1 = spectrace_ref.trace[0].data['Y']
                xref_o2 = spectrace_ref.trace[1].data['X']
                yref_o2 = spectrace_ref.trace[1].data['Y']

                # Use the solver on the background subtracted image.
                if subarray == 'SUBSTRIP96' or soss_filter == 'F277':
                    # Use only order 1 to solve theta, dx, dy
                    transform = solve_transform(scidata_bkg, scimask, xref_o1, yref_o1, soss_filter=soss_filter)
                else:
                    transform = solve_transform(scidata_bkg, scimask, xref_o1, yref_o1,
                                                xref_o2, yref_o2, soss_filter=soss_filter)

            log.info('Measured to Reference trace position transform: theta={:.4f}, dx={:.4f}, dy={:.4f}'.format(
                     transform[0], transform[1], transform[2]))

            # Prepare the reference file arguments.
            ref_file_args = get_ref_file_args(ref_files, transform)

            # TODO This is a temporary fix until the wave_grid is specified directly in model_image
            # Make sure wavelength maps cover only parts where the centroid is inside the detector image
            _mask_wv_map_centroid_outside(ref_file_args[0], ref_files, transform, scidata_bkg.shape[0])

            # Model the traces based on optics filter configuration (CLEAR or F277W)
            if soss_filter == 'CLEAR':

                # Model the image.
                kwargs = dict()
                kwargs['transform'] = transform
                kwargs['tikfac'] = soss_kwargs['tikfac']
                kwargs['n_os'] = soss_kwargs['n_os']
                kwargs['threshold'] = soss_kwargs['threshold']
                kwargs['devname'] = soss_kwargs['devname']

                result = model_image(scidata_bkg, scierr, scimask, refmask, ref_file_args, **kwargs)
                tracemodels, soss_kwargs['tikfac'], logl = result

            else:
                # No model can be fit
                # TODO: Implement model fitting for the F277W. Needs a specific F277W throughput ref file
                tracemodels = dict()

            # Save trace models for output reference
            for order in tracemodels:
                # Initialize a list for first integration
                if i == 0:
                    all_tracemodels[order] = []
                all_tracemodels[order].append(tracemodels[order])

            # Use the trace models to perform a de-contaminated extraction.
            kwargs = dict()
            kwargs['width'] = soss_kwargs['width']
            kwargs['devname'] = soss_kwargs['devname']
            kwargs['bad_pix'] = soss_kwargs['bad_pix']

            result = extract_image(scidata_bkg, scierr, scimask, tracemodels, ref_files, transform, subarray, **kwargs)
            wavelengths, fluxes, fluxerrs, npixels, box_weights = result

            # Save box weights for output reference
            for order in box_weights:
                # Initialize a list for first integration
                if i == 0:
                    all_box_weights[order] = []
                all_box_weights[order].append(box_weights[order])

            # Copy spectral data for each order into the output model.
            # TODO how to include parameters like transform and tikfac in the output.
            for order in wavelengths.keys():

                table_size = len(wavelengths[order])

                out_table = np.zeros(table_size, dtype=datamodels.SpecModel().spec_table.dtype)
                out_table['WAVELENGTH'] = wavelengths[order]
                out_table['FLUX'] = fluxes[order]
                out_table['FLUX_ERROR'] = fluxerrs[order]
                out_table['DQ'] = np.zeros(table_size)  # TODO how should these be set?
                out_table['BACKGROUND'] = col_bkg  # TODO this is a columnwise, per pixel, background value computed and subtracted before the solver, engine and box extraction.
                out_table['NPIXELS'] = npixels[order]

                spec = datamodels.SpecModel(spec_table=out_table)

                # Add integration number and spectral order
                spec.spectral_order = order_str_2_int[order]
                spec.int_num = i + 1  # integration number starts at 1, not 0 like python

                output_model.spec.append(spec)

    else:
        msg = "Only ImageModel and CubeModel are implemented for the NIRISS SOSS extraction."
        raise ValueError(msg)

    # Save output references
    for order in all_tracemodels:
        # Convert from list to array
        tracemod_ord = np.array(all_tracemodels[order])
        # Save
        order_int = order_str_2_int[order]
        setattr(output_references, f'order{order_int}', tracemod_ord)

    for order in all_box_weights:
        # Convert from list to array
        box_w_ord = np.array(all_box_weights[order])
        # Save
        order_int = order_str_2_int[order]
        setattr(output_references, f'aperture{order_int}', box_w_ord)

    return output_model, output_references
