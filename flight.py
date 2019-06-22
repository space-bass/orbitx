#!/usr/bin/env python3

"""
Main for CnC, the 'Command and Control' server

This is the central server that stores all state, serves it to networked
modules, and receives updates from networked modules.
"""

import argparse
import atexit
import copy
import logging
import os
from pathlib import Path
import time
import warnings
import concurrent.futures
import urllib.parse
from typing import Callable, List, Optional

import grpc

from orbitx import common
from orbitx import network
from orbitx import physics
from orbitx.graphics import flight_gui
import orbitx.orbitx_pb2_grpc as grpc_stubs

log = logging.getLogger()
cleanup_function: Optional[Callable] = None
ungraceful_shutdown_handler: Optional[Callable] = None


def parse_args():
    """Parse CLI arguments. Jupyter might add extra arguments, ignore them."""
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            'This program can be started in either "lead server" mode\n'
            'or "mirror" mode.\n'
            '\n'
            'In "lead server" mode, this program simulates a solar system\n'
            'using the flight data from the JSON savefile specified as a\n'
            'command-line argument to this program. This program will also\n'
            'serve physics data on the port specified by --serve-on-port.\n'
            'To activate "lead server" mode, pass "file:path/to/file" or\n'
            '"file:/absolute/path/to/file" as the data_location argument.\n'
            '\n'
            'In "mirror" mode, this program requests physics updates from\n'
            'the lead server specified by the hostname and optional port\n'
            'argument.\n'
            'To activate "mirror" mode, pass "mirror://..." as the\n'
            'data_location argument.'
        ))

    parser.add_argument('data_location', type=str, nargs='?',
                        default=('file:OCESS.json'),
                        help=(
                            'Where flight data is located. Accepts arguments'
                            ' of the form '
                            '"mirror://hostname" or "mirror://hostname:port" '
                            'or "file:path/to/save.json" or '
                            '"file:/absolute/path/to/save.json". '
                            'See help text for details. Defaults to '
                            'file:OCESS.json. Paths are relative to data/saves'
                        ))

    parser.add_argument('--serve-on-port', type=int, metavar='PORT',
                        help=(
                            'For a lead server, specifies which port to serve '
                            'physics data on. Specifying this for a mirror is '
                            ' an error.'
                        ))

    parser.add_argument('--no-gui', action='store_true', default=False,
                        help='Don\'t launch the flight GUI.')

    parser.add_argument('--no-intro', action='store_true', default=False,
                        help='Skip intro animation for quick start.')

    parser.add_argument('-v', '--verbose', action='store_true', default=False,
                        help='Logs everything to both logfile and output.')

    parser.add_argument('--profile', action='store_true', default=False,
                        help='Generating profiling reports, for a flamegraph.')

    args, unknown = parser.parse_known_args()
    if unknown:
        log.warning(f'Got unrecognized args: {unknown}')

    args.data_location = urllib.parse.urlparse(args.data_location)
    # Check that the data_location is well-formed
    assert args.data_location.scheme == 'file' or \
        args.data_location.scheme == 'mirror'

    if args.data_location.scheme == 'file':
        # We're in lead server mode
        assert not args.data_location.netloc
        assert args.data_location.path
        assert not args.data_location.query
        assert not args.data_location.fragment
        if not os.path.isabs(args.data_location.path):
            # Take relative paths relative to the data/saves/
            args.data_location = args.data_location._replace(
                path=common.savefile(args.data_location.path)
            )
        if args.serve_on_port is None:
            # We can't have a default value of this port, because we want to
            # check for its existence when we're in mirroring client mode
            args.serve_on_port = common.DEFAULT_LEAD_SERVER_PORT
    else:
        # We're in mirroring client mode
        assert args.serve_on_port is None  # Meaningless in this mode
        assert args.data_location.netloc
        assert not args.data_location.path
        assert not args.data_location.query
        assert not args.data_location.fragment
        if not args.data_location.port:
            # Port is optional. If it does not exist use the default port.
            args.data_location = args.data_location._replace(netloc=(
                args.data_location.hostname +
                ':' +
                str(common.DEFAULT_LEAD_SERVER_PORT)
            ))

    if args.verbose:
        common.enable_verbose_logging()

    return args


def lead_server_loop(args):
    """Main, 'while True'-style loop for a lead server. Blocking.
    See help text for command line arguments for what a lead server is."""

    # Before you make changes to the lead server architecture, consider that
    # the GRPC server runs in a separate thread than this thread!
    state_server = network.StateServer()

    log.info(f'Loading save at {args.data_location.path}')
    physics_engine = physics.PEngine(
        common.load_savefile(Path(args.data_location.path))
    )

    if not args.no_gui:
        global cleanup_function
        global ungraceful_shutdown_handler
        gui = flight_gui.FlightGui(physics_engine.get_state(),
                                   intro=(not args.no_intro),
                                   running_as_mirror=False)
        cleanup_function = gui.shutdown
        ungraceful_shutdown_handler = gui.ungraceful_shutdown

    server = grpc.server(
        concurrent.futures.ThreadPoolExecutor(max_workers=4))
    grpc_stubs.add_StateServerServicer_to_server(state_server, server)
    server.add_insecure_port(f'[::]:{args.serve_on_port}')
    server.start()  # This doesn't block!
    # Need a context manager from now on, to make sure the server always stops.
    with common.GrpcServerContext(server):
        log.info(f'Server running on port {args.serve_on_port}. Ctrl-C exits.')

        if args.profile:
            common.start_profiling()
        while True:
            user_commands: List[network.Request] = []
            state = physics_engine.get_state()
            state_server.notify_state_change(
                copy.deepcopy(state._proto_state))

            if not args.no_gui:
                user_commands += gui.pop_commands()
            user_commands += state_server.pop_commands()

            # If we have any commands, process them so the simthread has as
            # much time as possible to regenerate solutions before next update
            for command in user_commands:
                if command.ident == network.Request.NOOP:
                    continue
                log.info(f'Got command: {command}')
                physics_engine.handle_request(command)

            if not args.no_gui:
                gui.draw(state)
                gui.rate(common.FRAMERATE)
            else:
                time.sleep(1 / common.FRAMERATE)


def mirroring_loop(args):
    """Main, 'while True'-style loop for a mirroring client. Blocking.
    See help text for CLI arguments for the difference between mirroring and
    serving."""
    time_of_last_network_update = 0
    networking = True  # Whether data is requested over the network

    log.info(f'Connecting to lead server {args.data_location.geturl()}.')
    with network.StateClient(
        args.data_location.hostname, args.data_location.port
    ) as mirror_state:
        state = mirror_state()
        physics_engine = physics.PEngine(state)

        if not args.no_gui:
            gui = flight_gui.FlightGui(state,
                                       intro=(not args.no_intro),
                                       running_as_mirror=True)
            global cleanup_function
            cleanup_function = gui.shutdown

        while True:
            if not args.no_gui:
                old_networking = networking
                networking = gui.lead_server_communication_requested()
                if old_networking != networking:
                    log.info(
                        'Flight mirror is now ' +
                        ('networking' if networking else 'not networking') +
                        ' with the lead flight server at ' +
                        mirror_state.cnc_location)

            if (networking and
                time.monotonic() - time_of_last_network_update >
                    common.TIME_BETWEEN_NETWORK_UPDATES):
                state = mirror_state()
                physics_engine.set_state(state)
                time_of_last_network_update = time.monotonic()
            else:
                state = physics_engine.get_state()

            if not args.no_gui:
                gui.draw(state)
                if not networking:
                    # When we're not networking, allow user input.
                    user_commands = args.pop_commands()
                    for request in user_commands:
                        physics_engine.handle_request(request)
                gui.rate(common.FRAMERATE)
            else:
                time.sleep(1 / common.FRAMERATE)


def log_git_info():
    """For ease in debugging, try to get some version information.
    This should never throw a fatal error, it's just nice-to-know stuff."""
    try:
        git_dir = Path('.git')
        head_file = git_dir / 'HEAD'
        with head_file.open() as f:
            head_contents = f.readline().strip()
            log.info(f'Contents of .git/HEAD: {head_contents}')
        if head_contents.split()[0] == 'ref:':
            hash_file = git_dir / head_contents.split()[1]
            with hash_file.open() as f:
                log.info(f'Current reference hash: {f.readline().strip()}')
    except FileNotFoundError:
        return


def main():
    """Delegate work to either lead_server_loop or mirroring_loop."""
    # vpython uses deprecated python features, but we shouldn't get a fatal
    # exception if we try to use vpython. DeprecationWarnings are normally
    # enabled when __name__ == __main__
    warnings.filterwarnings('once', category=DeprecationWarning)
    # vpython generates other warnings, as well as its use of asyncio
    warnings.filterwarnings('ignore', category=ResourceWarning)
    warnings.filterwarnings('ignore', module='vpython|asyncio|autobahn')

    log_git_info()

    args = parse_args()
    try:
        if args.data_location.scheme == 'file':
            lead_server_loop(args)
        else:
            assert args.data_location.scheme == 'mirror'
            mirroring_loop(args)
    except KeyboardInterrupt:
        # We're expecting ctrl-C will end the program, hide the exception from
        # the user.
        pass
    except Exception as e:
        log.exception('Unexpected exception, exiting...')
        atexit.unregister(common.print_handler_cleanup)
        if ungraceful_shutdown_handler is not None:
            ungraceful_shutdown_handler()

        if isinstance(e, (AttributeError, ValueError)):
            proto_file = Path('orbitx', 'orbitx.proto')
            generated_file = Path('orbitx', 'orbitx_pb2.py')
            if not generated_file.is_file():
                log.warning('================================================')
                log.warning(f'{proto_file} does not exist.')
            elif proto_file.stat().st_mtime > generated_file.stat().st_mtime:
                log.warning('================================================')
                log.warning(f'{proto_file} is newer than {generated_file}.')
            else:
                # We thought that generated protobuf definitions were out of
                # date, but it doesn't actually look like that's the case.
                # Raise the exception normally.
                raise

            log.warning('A likely fix for this fatal exception is to run the')
            log.warning('`build` target of orbitx/Makefile, or at least')
            log.warning('copy-pasting the contents of the `build` target and')
            log.warning('running it in your shell.')
            log.warning('You\'ll have to do this every time you change')
            log.warning(str(proto_file))
            log.warning('================================================')

        raise
    finally:
        if cleanup_function is not None:
            cleanup_function()


if __name__ == '__main__':
    main()
