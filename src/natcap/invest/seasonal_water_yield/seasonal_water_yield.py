"""InVEST Seasonal Water Yield Model"""

import os
import logging
import re
import fractions

import scipy.special
import numpy
import gdal
import pygeoprocessing
import pygeoprocessing.routing

import seasonal_water_yield_core

logging.basicConfig(format='%(asctime)s %(name)-20s %(levelname)-8s \
%(message)s', level=logging.DEBUG, datefmt='%m/%d/%Y %H:%M:%S ')

LOGGER = logging.getLogger(
    'natcap.invest.seasonal_water_yield.seasonal_water_yield')

N_MONTHS = 12


def execute(args):
    """This function invokes the InVEST seasonal water yield model described in
    "Spatial attribution of baseflow generation at the parcel level for
    ecosystem-service valuation", Guswa, et. al (under review in Water
    "Resources Research")

    Parameters:
        args['workspace_dir'] (string): output directory for intermediate,
        temporary, and final files
        args['results_suffix'] (string): (optional) string to append to any
            output files
        args['threshold_flow_accumulation'] (number): used when classifying
            stream pixels from the DEM by thresholding the number of upstream
            cells that must flow int a cell before it's considered
            part of a stream.
        args['et0_dir'] (string): required if args['user_defined_local_recharge'] is
            False.  Path to a directory that contains rasters of
            monthly reference evapotranspiration; units in mm.
        args['precip_dir'] (string): required if args['user_defined_local_recharge']
            is False. A path to a directory that contains rasters of monthly
            precipitation; units in mm.
        args['dem_path'] (string): a path to a digital elevation raster
        args['lulc_path'] (string): a path to a land cover raster used to
            classify biophysical properties of pixels.
        args['soil_group_path'] (string): required if
            args['user_defined_local_recharge'] is  False. A path to a raster
            indicating SCS soil groups where integer values are mapped to soil
            types:
                1: A
                2: B
                3: C
                4: D
        args['aoi_path'] (string): path to a vector that indicates the area over
            which the model should be run, as well as the area in which to
            aggregate over when calculating the output Qb.
        args['biophysical_table_path'] (string): path to a CSV table that maps
            landcover codes paired with soil group types to curve numbers as
            well as Kc values.  Headers must be 'lucode', 'CN_A', 'CN_B',
            'CN_C', 'CN_D', and 'Kc'.
        args['rain_events_table_path'] (string): Required if
            args['user_defined_local_recharge'] is  False. Path to a CSV table that
            has headers 'month' (1-12) and 'events' (int >= 0) that indicates
            the number of rain events per month
        args['alpha_m'] (float or string): proportion of upslope annual
            available local recharge that is available in month m.
        args['beta_i'] (float or string): is the fraction of the upgradient
            subsidy that is available for downgradient evapotranspiration.
        args['gamma'] (float or string): is the fraction of pixel local recharge that
            is available to downgradient pixels.
        args['user_defined_local_recharge'] (boolean): if True, indicates user will
            provide pre-defined local recharge raster layer
        args['local_recharge_path'] (string): required if
            args['user_defined_local_recharge'] is True.  If provided pixels indicate
            the amount of local recharge; units in mm.
    """

    # prepare and test inputs for common errors
    alpha_m = float(fractions.Fraction(args['alpha_m']))
    beta_i = float(fractions.Fraction(args['beta_i']))
    gamma = float(fractions.Fraction(args['gamma']))
    threshold_flow_accumulation = float(args['threshold_flow_accumulation'])

    try:
        file_suffix = args['results_suffix']
        if file_suffix != "" and not file_suffix.startswith('_'):
            file_suffix = '_' + file_suffix
    except KeyError:
        file_suffix = ''

    pygeoprocessing.geoprocessing.create_directories([args['workspace_dir']])

    output_file_registry = {
        'aet_path': os.path.join(args['workspace_dir'], 'aet.tif'),
        'cn_path': os.path.join(args['workspace_dir'], 'cn.tif'),
        'flow_accum_path': os.path.join(args['workspace_dir'], 'flow_accum.tif'),
        'flow_dir_path': os.path.join(args['workspace_dir'], 'flow_dir.tif'),
        'kc_path': os.path.join(args['workspace_dir'], 'kc.tif'),
        'l_avail_path': os.path.join(args['workspace_dir'], 'L_avail.tif'),
        'l_path': os.path.join(args['workspace_dir'], 'L.tif'),
        'l_sum_avail_path': os.path.join(args['workspace_dir'], 'L_sum_avail.tif'),
        'outflow_direction_path': os.path.join(args['workspace_dir'], 'outflow_direction.tif'),
        'outflow_weights_path': os.path.join(args['workspace_dir'], 'outflow_weights.tif'),
        'qb_out_path': os.path.join(args['workspace_dir'], 'qb.txt'),
        'qfi_path': os.path.join(args['workspace_dir'], 'qf.tif'),
        'l_sum_avail_pour_path': os.path.join(args['workspace_dir'], 'L_sum_avail_pour.tif'),
        'sf_down_path': os.path.join(args['workspace_dir'], 'sf_down.tif'),
        'sf_path': os.path.join(args['workspace_dir'], 'sf.tif'),
        'si_path': os.path.join(args['workspace_dir'], 'si.tif'),
        'stream_path': os.path.join(args['workspace_dir'], 'stream.tif'),
        'vri_path': os.path.join(args['workspace_dir'], 'vri.tif'),
        }

    # add a suffix to all the output files
    for file_id in output_file_registry:
        output_file_registry[file_id] = file_suffix.join(
            os.path.splitext(output_file_registry[file_id]))

    # this variable is only needed if there is not a predefined recharge file
    if not args['user_defined_local_recharge']:
        output_file_registry['local_recharge_path'] = os.path.join(
            args['workspace_dir'], 'local_recharge%s.tif' % file_suffix)
        output_file_registry['local_recharge_avail_path'] = os.path.join(
            args['workspace_dir'], 'local_recharge_avail%s.tif' % file_suffix)

    temporary_file_registry = {
        'lulc_aligned_path': pygeoprocessing.temporary_filename(),
        'dem_aligned_path': pygeoprocessing.temporary_filename(),
        'loss_path': pygeoprocessing.geoprocessing.temporary_filename(),
        'zero_absorption_source_path': (
            pygeoprocessing.geoprocessing.temporary_filename()),
        'soil_group_aligned_path': pygeoprocessing.temporary_filename()
    }

    if args['user_defined_local_recharge']:
        temporary_file_registry['local_recharge_aligned_path'] = (
            pygeoprocessing.geoprocessing.temporary_filename())

    pixel_size = pygeoprocessing.geoprocessing.get_cell_size_from_uri(
        args['lulc_path'])

    LOGGER.info('Aligning and clipping dataset list')
    input_align_list = [args['lulc_path'], args['dem_path']]
    output_align_list = [
        temporary_file_registry['lulc_aligned_path'],
        temporary_file_registry['dem_aligned_path'],
        ]

    if not args['user_defined_local_recharge']:
        precip_path_list = []
        et0_path_list = []

        et0_dir_list = [
            os.path.join(args['et0_dir'], f) for f in os.listdir(
                args['et0_dir'])]
        precip_dir_list = [
            os.path.join(args['precip_dir'], f) for f in os.listdir(
                args['precip_dir'])]

        qf_monthly_path_list = []
        for m_index in range(1, N_MONTHS + 1):
            qf_monthly_path_list.append(
                os.path.join(
                    args['workspace_dir'], 'qf_%d%s.tif' %
                    (m_index, file_suffix)))

        for month_index in range(1, N_MONTHS + 1):
            month_file_match = re.compile(r'.*[^\d]%d\.[^.]+$' % month_index)

            for data_type, dir_list, path_list in [
                    ('et0', et0_dir_list, et0_path_list),
                    ('Precip', precip_dir_list, precip_path_list)]:

                file_list = [x for x in dir_list if month_file_match.match(x)]
                if len(file_list) == 0:
                    raise ValueError(
                        "No %s found for month %d" % (data_type, month_index))
                if len(file_list) > 1:
                    raise ValueError(
                        "Ambiguous set of files found for month %d: %s" %
                        (month_index, file_list))
                path_list.append(file_list[0])

        #pre align all the datasets
        precip_path_aligned_list = [
            pygeoprocessing.geoprocessing.temporary_filename() for _ in
            range(len(precip_path_list))]
        et0_path_aligned_list = [
            pygeoprocessing.geoprocessing.temporary_filename() for _ in
            range(len(precip_path_list))]
        input_align_list = (
            precip_path_list + [args['soil_group_path']] + et0_path_list +
            input_align_list)
        output_align_list = (
            precip_path_aligned_list +
            [temporary_file_registry['soil_group_aligned_path']] +
            et0_path_aligned_list + output_align_list)

    interpolate_list = ['nearest'] * len(input_align_list)
    align_index = 0
    if args['user_defined_local_recharge']:
        input_align_list.append(args['local_recharge_path'])
        output_align_list.append(
            temporary_file_registry['local_recharge_aligned_path'])
        interpolate_list.append('nearest')
        align_index = len(interpolate_list) - 1

    pygeoprocessing.geoprocessing.align_dataset_list(
        input_align_list, output_align_list, interpolate_list, pixel_size,
        'intersection', align_index, aoi_uri=args['aoi_path'],
        assert_datasets_projected=True)

    LOGGER.info('calc flow direction')
    pygeoprocessing.routing.flow_direction_d_inf(
        temporary_file_registry['dem_aligned_path'],
        output_file_registry['flow_dir_path'])

    LOGGER.info('calc flow accumulation')
    pygeoprocessing.routing.flow_accumulation(
        output_file_registry['flow_dir_path'],
        temporary_file_registry['dem_aligned_path'],
        output_file_registry['flow_accum_path'])
    pygeoprocessing.routing.stream_threshold(
        output_file_registry['flow_accum_path'],
        threshold_flow_accumulation,
        output_file_registry['stream_path'])

    LOGGER.info('calculating flow weights')
    seasonal_water_yield_core.calculate_flow_weights(
        output_file_registry['flow_dir_path'],
        output_file_registry['outflow_weights_path'],
        output_file_registry['outflow_direction_path'])

    LOGGER.info('classifying kc')
    biophysical_table = pygeoprocessing.geoprocessing.get_lookup_from_table(
        args['biophysical_table_path'], 'lucode')
    kc_lookup = dict([
        (lucode, biophysical_table[lucode]['kc']) for lucode in
        biophysical_table])

    pygeoprocessing.geoprocessing.reclassify_dataset_uri(
        temporary_file_registry['lulc_aligned_path'], kc_lookup,
        output_file_registry['kc_path'], gdal.GDT_Float32, -1)

    LOGGER.info('calculate slow flow')
    if not args['user_defined_local_recharge']:
        LOGGER.info('loading number of monthly events')
        rain_events_lookup = (
            pygeoprocessing.geoprocessing.get_lookup_from_table(
                args['rain_events_table_path'], 'month'))
        n_events = dict([
            (month, rain_events_lookup[month]['events'])
            for month in rain_events_lookup])

        LOGGER.info('calculating curve number')
        _calculate_curve_number_raster(
            temporary_file_registry['lulc_aligned_path'],
            temporary_file_registry['soil_group_aligned_path'],
            biophysical_table, pixel_size, output_file_registry['cn_path'])

        _calculate_si_raster(
            output_file_registry['cn_path'],
            output_file_registry['si_path'],
            output_file_registry['stream_path'])

        for month_index in xrange(N_MONTHS):
            LOGGER.info('calculate quick flow for month %d', month_index+1)
            _calculate_monthly_quick_flow(
                precip_path_aligned_list[month_index],
                temporary_file_registry['lulc_aligned_path'],
                output_file_registry['cn_path'], n_events[month_index+1],
                output_file_registry['stream_path'],
                qf_monthly_path_list[month_index],
                output_file_registry['si_path'])

        qf_nodata = -1
        LOGGER.info('calculating QFi')

        def qfi_sum_op(*qf_values):
            """sum the monthly qfis"""
            qf_sum = qf_values[0].copy()
            for index in range(1, len(qf_values)):
                qf_sum += qf_values[index]
            qf_sum[qf_values[0] == qf_nodata] = qf_nodata
            return qf_sum
        pygeoprocessing.geoprocessing.vectorize_datasets(
            qf_monthly_path_list, qfi_sum_op, output_file_registry['qfi_path'],
            gdal.GDT_Float32, qf_nodata, pixel_size, 'intersection',
            vectorize_op=False, datasets_are_pre_aligned=True)

        seasonal_water_yield_core.calculate_local_recharge(
            precip_path_aligned_list, et0_path_aligned_list,
            qf_monthly_path_list, output_file_registry['flow_dir_path'],
            output_file_registry['outflow_weights_path'],
            output_file_registry['outflow_direction_path'],
            temporary_file_registry['dem_aligned_path'],
            temporary_file_registry['lulc_aligned_path'], kc_lookup, alpha_m,
            beta_i, gamma, output_file_registry['stream_path'],
            output_file_registry['local_recharge_path'],
            output_file_registry['local_recharge_avail_path'],
            output_file_registry['l_sum_avail_path'],
            output_file_registry['aet_path'], output_file_registry['kc_path'])
    else:
        output_file_registry['local_recharge_path'] = (
            local_recharge_aligned_path)
        local_recharge_nodata = (
            pygeoprocessing.geoprocessing.get_nodata_from_uri(
                output_file_registry['local_recharge_path']))

        def calc_local_recharge_avail(local_recharge_array):
            local_recharge_threshold = local_recharge_array * gamma
            local_recharge_threshold[local_recharge_threshold < 0] = 0.0
            return numpy.where(
                local_recharge_array != local_recharge_nodata,
                local_recharge_threshold, local_recharge_nodata)

        #calc local_recharge avail
        pygeoprocessing.geoprocessing.vectorize_datasets(
            [output_file_registry['local_recharge_aligned_path']],
            calc_local_recharge_avail, output_file_registry['local_recharge_avail_path'],
            gdal.GDT_Float32, local_recharge_nodata, pixel_size, 'intersection',
            vectorize_op=False, datasets_are_pre_aligned=True)

        #calc r_sum_avail with flux accumulation
        pygeoprocessing.make_constant_raster_from_base_uri(
            temporary_file_registry['dem_aligned_path'], 0.0,
            temporary_file_registry['zero_absorption_source_path'])

        pygeoprocessing.routing.route_flux(
            output_file_registry['flow_dir_path'],
            temporary_file_registry['dem_path_aligned'],
            output_file_registry['local_recharge_avail_path'],
            temporary_file_registry['zero_absorption_source_path'],
            temporary_file_registry['loss_path'],
            output_file_registry['l_sum_avail_path'], 'flux_only',
            include_source=False)

    #calculate Qb as the sum of local_recharge_avail over the aoi
    qb_results = pygeoprocessing.geoprocessing.aggregate_raster_values_uri(
        output_file_registry['local_recharge_path'], args['aoi_path'])
    qb_result = qb_results.total[9999] / qb_results.n_pixels[9999]
    #9999 is the value used to index fields if no shapefile ID is provided
    qb_file = open(output_file_registry['qb_out_path'], 'w')
    qb_file.write("%f\n" % qb_result)
    qb_file.close()
    LOGGER.info("Qb = %f", qb_result)

    pixel_size = pygeoprocessing.geoprocessing.get_cell_size_from_uri(
        output_file_registry['local_recharge_path'])
    ri_nodata = pygeoprocessing.geoprocessing.get_nodata_from_uri(
        output_file_registry['local_recharge_path'])

    def vri_op(ri_array):
        """calc vri index"""
        return numpy.where(
            ri_array != ri_nodata, ri_array / qb_result, ri_nodata)

    pygeoprocessing.geoprocessing.vectorize_datasets(
        [output_file_registry['local_recharge_path']], vri_op,
        output_file_registry['vri_path'], gdal.GDT_Float32, ri_nodata,
        pixel_size, 'intersection', vectorize_op=False,
        datasets_are_pre_aligned=True)

    LOGGER.info('calculating l_sum_avail_pour')
    seasonal_water_yield_core.calculate_r_sum_avail_pour(
        output_file_registry['l_sum_avail_path'],
        output_file_registry['outflow_weights_path'],
        output_file_registry['outflow_direction_path'],
        output_file_registry['l_sum_avail_pour_path'])

    LOGGER.info('calculating slow flow')
    seasonal_water_yield_core.route_sf(
        temporary_file_registry['dem_aligned_path'],
        output_file_registry['local_recharge_avail_path'],
        output_file_registry['l_sum_avail_path'],
        output_file_registry['l_sum_avail_pour_path'],
        output_file_registry['outflow_direction_path'],
        output_file_registry['outflow_weights_path'],
        output_file_registry['stream_path'],
        output_file_registry['sf_path'],
        output_file_registry['sf_down_path'])

    LOGGER.info('  (\\w/)  SWY Complete!')
    LOGGER.info('  (..  \\ ')
    LOGGER.info(' _/  )  \\______')
    LOGGER.info('(oo /\'\\        )`,')
    LOGGER.info(' `--\' (v  __( / ||')
    LOGGER.info('       |||  ||| ||')
    LOGGER.info('      //_| //_|')


def _calculate_monthly_quick_flow(
        precip_path, lulc_path, cn_path, n_events, stream_path,
        qf_monthly_path, si_path):
    """Calculates quick flow for a month

    Parameters:
        precip_path_list (list of string): list of paths to files that
            correspond to precipitation per month.  Files should be in order of
            increasing month, although the final calculation is not affected if
            not.
        lulc_path (string): path to landcover raster
        cn_path (string): path to curve number raster
        n_events (dict of int -> int): maps the number of rain events per month
            where the index to n_events is the calendar month starting at 1.
        stream_path (string): path to stream mask raster where 1 indicates a
            stream pixel, 0 is a non-stream but otherwise valid area from the
            original DEM, and nodata indicates areas outside the valid DEM.
        qf_monthly_path_list (list of string): list of paths to output monthly
            rasters.
        si_path (string): list to output raster for potential maximum retention
        """

    si_nodata = -1
    cn_nodata = pygeoprocessing.geoprocessing.get_nodata_from_uri(cn_path)

    def si_op(ci_array, stream_array):
        """potential maximum retention"""
        si_array = 1000.0 / ci_array - 10
        si_array = numpy.where(ci_array != cn_nodata, si_array, si_nodata)
        si_array[stream_array == 1] = 0
        return si_array

    LOGGER.info('calculating Si')
    pixel_size = pygeoprocessing.geoprocessing.get_cell_size_from_uri(
        lulc_path)
    pygeoprocessing.geoprocessing.vectorize_datasets(
        [cn_path, stream_path], si_op, si_path, gdal.GDT_Float32,
        si_nodata, pixel_size, 'intersection', vectorize_op=False,
        datasets_are_pre_aligned=True)

    qf_nodata = -1
    p_nodata = pygeoprocessing.geoprocessing.get_nodata_from_uri(precip_path)
    def qf_op(p_im, s_i, stream_array):
        """Calculate quickflow as in equation 1a in user's guide

        Parameters:
            p_im (numpy.array): precipitation at pixel i on month m
            s_i (numpy.array): factor that is 1000/CN_i - 10
                (Equation 1b from user's guide)
            stream_mask (numpy.array): 1 if stream, otherwise not a stream
                pixel.

        Returns:
            quickflow (numpy.array)"""

        nodata_mask = (p_im == p_nodata) | (s_i == si_nodata)

        #a_im is the mean rain depth on a rainy day at pixel i on month m
        a_im = p_im / n_events / 25.4

        #qf_im is the quickflow at pixel i on month m (Equation 1a in the
        # user's guide)
        qf_im = (25.4 * n_events * (
            (a_im - s_i) * numpy.exp(-0.2 * s_i / a_im) +
            s_i ** 2 / a_im * numpy.exp((0.8 * s_i) / a_im) *
            scipy.special.expn(1, s_i / a_im)))

        # in cases where precipitation is small, a_im will be small and
        # the quickflow can get into an inf / 0.0 state.  This zeros the
        # result so at least we don't get a block of nodata pixels out
        qf_im[numpy.isnan(qf_im)] = 0.0

        # if a_im == 0, then QF should be zero
        qf_im[a_im == 0] = 0.0
        # mask out nodata
        qf_im[nodata_mask] = qf_nodata

        # if we're on a stream, set quickflow to the precipitation
        qf_im[stream_array == 1] = p_im[stream_array == 1]
        return qf_im

    pygeoprocessing.geoprocessing.vectorize_datasets(
        [precip_path, si_path, stream_path], qf_op, qf_monthly_path,
        gdal.GDT_Float32, qf_nodata, pixel_size, 'intersection',
        vectorize_op=False, datasets_are_pre_aligned=True)

def _calculate_curve_number_raster(
        lulc_path, soil_group_path, biophysical_table, pixel_size, cn_path):
    """Calculate the CN raster from the landcover and soil group rasters"""

    soil_nodata = pygeoprocessing.get_nodata_from_uri(soil_group_path)
    map_soil_type_to_header = {
        1: 'cn_a',
        2: 'cn_b',
        3: 'cn_c',
        4: 'cn_d',
    }
    cn_nodata = -1
    lulc_to_soil = {}
    lulc_nodata = pygeoprocessing.get_nodata_from_uri(lulc_path)
    for soil_id, soil_column in map_soil_type_to_header.iteritems():
        lulc_to_soil[soil_id] = {
            'lulc_values': [],
            'cn_values': []
        }
        for lucode in sorted(biophysical_table.keys() + [lulc_nodata]):
            try:
                lulc_to_soil[soil_id]['cn_values'].append(
                    biophysical_table[lucode][soil_column])
                lulc_to_soil[soil_id]['lulc_values'].append(lucode)
            except KeyError:
                if lucode == lulc_nodata:
                    lulc_to_soil[soil_id]['lulc_values'].append(lucode)
                    lulc_to_soil[soil_id]['cn_values'].append(cn_nodata)
                else:
                    raise
        lulc_to_soil[soil_id]['lulc_values'] = (
            numpy.array(lulc_to_soil[soil_id]['lulc_values'],
                        dtype=numpy.int32))
        lulc_to_soil[soil_id]['cn_values'] = (
            numpy.array(lulc_to_soil[soil_id]['cn_values'],
                        dtype=numpy.float32))

    def cn_op(lulc_array, soil_group_array):
        """map lulc code and soil to a curve number"""
        cn_result = numpy.empty(lulc_array.shape)
        cn_result[:] = cn_nodata
        for soil_group_id in numpy.unique(soil_group_array):
            if soil_group_id == soil_nodata:
                continue
            current_soil_mask = (soil_group_array == soil_group_id)
            index = numpy.digitize(
                lulc_array.ravel(),
                lulc_to_soil[soil_group_id]['lulc_values'], right=True)
            cn_values = (
                lulc_to_soil[soil_group_id]['cn_values'][index]).reshape(
                    lulc_array.shape)
            cn_result[current_soil_mask] = cn_values[current_soil_mask]
        return cn_result

    cn_nodata = -1
    pygeoprocessing.vectorize_datasets(
        [lulc_path, soil_group_path], cn_op, cn_path, gdal.GDT_Float32,
        cn_nodata, pixel_size, 'intersection', vectorize_op=False,
        datasets_are_pre_aligned=True)


def _calculate_si_raster(cn_path, si_path, stream_path):
    """Calculates the S factor of the SCS Runoff equation also known as the
    potential maximum retention.

    Parameters:
        cn_path (string): path to curve number raster
        lulc_path (string): path to landcover raster
        si_path (string): path to output s_i raster

    Returns:
        None
    """

    si_nodata = -1
    cn_nodata = pygeoprocessing.geoprocessing.get_nodata_from_uri(cn_path)

    def si_op(ci_factor, stream_mask):
        """calculate si factor"""
        valid_mask = (ci_factor != cn_nodata) & (ci_factor > 0)
        si_array = numpy.empty(ci_factor.shape)
        si_array[:] = si_nodata
        # multiply by the stream mask != 1 so we get 0s on the stream and
        # unaffected results everywhere else
        si_array[valid_mask] = (
            (1000.0 / ci_factor[valid_mask] - 10) * (
                stream_mask[valid_mask] != 1))
        return si_array

    pixel_size = pygeoprocessing.geoprocessing.get_cell_size_from_uri(cn_path)
    pygeoprocessing.geoprocessing.vectorize_datasets(
        [cn_path, stream_path], si_op, si_path, gdal.GDT_Float32,
        si_nodata, pixel_size, 'intersection', vectorize_op=False,
        datasets_are_pre_aligned=True)
