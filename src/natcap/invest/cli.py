"""
Single entry point for all InVEST applications.
"""
from __future__ import absolute_import

import argparse
import os
import importlib
import logging
import sys
import collections
import pprint
from datetime import datetime

from . import utils

LOGGER = logging.getLogger(__name__)
_UIMETA = collections.namedtuple('UIMeta', 'pyname gui')

_MODEL_UIS = {
    'pollination': _UIMETA(
        pyname='natcap.invest.pollination.pollination',
        gui='pollination.Pollination'),
    'habitat_suitability': _UIMETA(
        pyname='natcap.invest.habitat_suitability',
        gui=None),
}


def _import_ui_class(gui_class):
    mod_name, classname = gui_class.split('.')
    module = importlib.import_module(
        name='.ui.%s' % mod_name,
        package='natcap.invest')
    return getattr(module, classname)

# metadata for models: full modelname, first released, full citation,
# local documentation name.


# Goal: allow InVEST models to be run at the command-line, without a UI.
#   problem: how to identify which models have Qt UIs available?
#       1.  If we can't import natcap.ui, we don't have any qt uis.
#       2.  We could iterate through all the model UI files and Identify the
#           model from its name and attributes.
#       3.  We could access a pre-processed list of models available, perhaps
#           written to a file during the setuptools build step.
#   problem: how to identify which models are available to the API?
#       1.  Recursively import natcap.invest and look for modules with execute
#           functions available.
#       2.  Import all of the execute functions to a known place (an __init__
#           file?).
#   problem: how to provide parameters?
#       1.  If execute is parseable, just extract parameters from the docstring
#           and allow each param to be provided as a CLI flag.
#       2.  Allow parameters to be passed as a JSON file
#       3.  Allow model to run with a scenario file.
#       PS: Optionally, don't validate inputs, but do validate by default.


def list_models():
    return sorted(_MODEL_UIS.keys())


def format_models():
    """
    Pretty-print available models.
    """
    print 'Available models:'
    model_names = list_models()
    max_model_name_length = max(len(name) for name in model_names)
    template_string = '    {modelname}   {usage}'
    strings = []
    for model_name in list_models():
        usage_string = '(No GUI available)'
        if _MODEL_UIS[model_name].gui is not None:
            usage_string = ''

        strings.append(template_string.format(
                modelname=model_name.ljust(max_model_name_length),
                usage=usage_string))
    return strings


class ListModelsAction(argparse.Action):
    def __init__(self,
                 option_strings,
                 dest,
                 default=False,
                 required=False,
                 help=None, *args, **kwargs):
        super(ListModelsAction, self).__init__(
            option_strings=option_strings,
            dest=dest,
            const=True,
            nargs=0,
            default=default,
            required=required,
            help=help, *args, **kwargs)

    def __call__(self, parser, namespace, values, option_string):
        setattr(namespace, self.dest, self.const)
        parser.exit(message='\n'.join(format_models()))


class SelectModelAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string):
        if values in ['', None]:
            parser.print_help()
            print format_models()
            parser.exit()
        else:
            known_models = list_models()
            matching_models = [model for model in known_models if
                               model.startswith(values)]

            exact_matches = [model for model in known_models if
                             model == values]

            if len(matching_models) == 1:
                modelname = matching_models[0]
            elif len(exact_matches) == 1:
                modelname = exact_matches[0]
            elif len(matching_models) == 0:
                parser.exit("Error: '%s' not a known model" % values)
            else:
                parser.exit((
                    "Model string '{model}' is ambiguous:\n"
                    "    {matching_models}").format(
                        model=values,
                        matching_models=' '.join(matching_models)))
        setattr(namespace, self.dest, modelname)



def write_console_files(out_dir, extension):
    """
    Write out console files for each of the target models to the output dir.

    Parameters:
        out_dir: The directory in which to save the console files.
        extension: The extension of the output files (e.g. 'bat', 'sh')

    Returns:
        Nothing.  Writes files to out_dir, though.
    """
    content_template = "invest %(model)s\n"
    filename_template = os.path.join(out_dir, "invest_%(modelname)s_.%(ext)s")
    for model_name in list_models():
        console_filepath = filename_template % {
            'modelname': model_name, 'ext': extension}
        console_file = open(console_filepath)
        console_file.write(content_template % {'model': model_name})
        console_file.close()


def main():
    """
    Single entry point for all InVEST model user interfaces.

    This function provides a CLI for calling InVEST models, though it it very
    primitive.  Apart from displaying a help messsage and the version, this
    function will also (optionally) list the known models (based on the found
    json filenames) and will fire up an IUI interface based on the model name
    provided.
    """

    parser = argparse.ArgumentParser(description=(
        'Integrated Valuation of Ecosystem Services and Tradeoffs.  '
        'InVEST (Integrated Valuation of Ecosystem Services and Tradeoffs) is '
        'a family of tools for quantifying the values of natural capital in '
        'clear, credible, and practical ways. In promising a return (of '
        'societal benefits) on investments in nature, the scientific community '
        'needs to deliver knowledge and tools to quantify and forecast this '
        'return. InVEST enables decision-makers to quantify the importance of '
        'natural capital, to assess the tradeoffs associated with alternative '
        'choices, and to integrate conservation and human development.  \n\n'
        'Older versions of InVEST ran as script tools in the ArcGIS ArcToolBox '
        'environment, but have almost all been ported over to a purely '
        'open-source python environment.'),
        prog='invest'
    )
    list_group = parser.add_mutually_exclusive_group()
    verbosity_group = parser.add_mutually_exclusive_group()
    import natcap.invest

    parser.add_argument('--version', action='version',
                        version=natcap.invest.__version__)
    list_group.add_argument('--list', action=ListModelsAction,
                            help='List available models')
    verbosity_group.add_argument('--debug', dest='log_level',
                                 default=logging.CRITICAL,
                                 action='store_const', const=logging.DEBUG,
                                 help='Enable debug logging. Alias for -vvvvv')
    parser.add_argument('--test', action='store_false',
                        help='Run in headless mode with default args.')
    parser.add_argument('--scenario', '-s', default=None, nargs='?',
                        help='Run the specified model with this scenario')
    verbosity_group.add_argument('--verbose', '-v', dest='verbosity', default=0,
                                 action='count', help=('Increase verbosity'))
    list_group.add_argument('model', action=SelectModelAction, nargs='?',
                            help=('The model/tool to run. Use --list to show '
                                  'available models/tools. Identifiable model '
                                  'prefixes may also be used.'))

    args = parser.parse_args()

    root_logger = logging.getLogger()
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        fmt='%(asctime)s %(name)-18s %(levelname)-8s %(message)s',
        datefmt='%m/%d/%Y %H:%M:%S ')
    handler.setFormatter(formatter)

    # Set the log level based on what the user provides in the available
    # arguments.  Verbosity: the more v's the lower the logging threshold.
    # If --debug is used, the logging threshold is 10.
    # If the user goes lower than logging.DEBUG, default to logging.DEBUG.
    log_level = min(args.log_level, logging.CRITICAL - (args.verbosity*10))
    handler.setLevel(max(log_level, logging.DEBUG))  # don't go lower than DEBUG
    root_logger.addHandler(handler)
    LOGGER.info('Setting handler log level to %s', log_level)

    # FYI: Root logger by default has a level of logging.WARNING.
    # To capture ALL logging produced in this system at runtime, use this:
    # logging.getLogger().setLevel(logging.DEBUG)
    # Also FYI: using logging.DEBUG means that the logger will defer to
    # the setting of the parent logger.
    logging.getLogger('natcap').setLevel(logging.DEBUG)

    # Now that we've set up logging based on args, we can start logging.
    LOGGER.debug(args)

    try:
        import natcap.ui
    except ImportError:
        print ('Error: natcap.ui not installed:\n'
                '    pip install natcap.invest[ui]')
        return 3

    if args.scenario:
        from natcap.invest import scenarios
        target_mod = _MODEL_UIS[args.model].pyname
        model_module = importlib.import_module(name=target_mod)
        LOGGER.info('imported target %s from %s',
                    model_module.__name__, model_module)

        paramset = scenarios.read_parameter_set(args.scenario)

        with utils.capture_gdal_logging():
            warnings = []
            try:
                warnings = getattr(target_mod, 'validate')(paramset.args)
            except AttributeError:
                LOGGER.warn(
                    '%s does not have a defined validation function.',
                    model_module.LABEL)
            finally:
                if warnings:
                    LOGGER.warn('Warnings found: \n%s',
                                pprint.pformat(warnings))

            if not os.path.exists(paramset.args['workspace_dir']):
                os.makedirs(paramset.args['workspace_dir'])

            logfile = os.path.join(
                paramset.args['workspace_dir'],
                'InVEST-{modelname}-log-{timestamp}.txt'.format(
                    modelname='-'.join(model_module.LABEL.split(' ')),
                    timestamp=datetime.now().strftime("%Y-%m-%d--%H_%M_%S")))

            with utils.log_to_file(logfile):
                with utils.sandbox_tempdir(dir=paramset.args['workspace_dir'],
                                           set_tempdir=True):
                    getattr(model_module, 'execute')(paramset.args)
    else:
        model_classname = _import_ui_class(_MODEL_UIS[args.model].gui)
        model_form = model_classname()
        model_form.run()

if __name__ == '__main__':
    main()
