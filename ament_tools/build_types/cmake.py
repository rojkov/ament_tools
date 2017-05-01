# Copyright 2014 Open Source Robotics Foundation, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Implements the BuildType support for cmake based ament packages."""

import os
import sys

from ament_package.templates import get_environment_hook_template_path

from ament_tools.build_type import BuildAction
from ament_tools.build_type import BuildType

from ament_tools.context import ContextExtender

from ament_tools.helper import compute_deploy_destination
from ament_tools.helper import deploy_file
from ament_tools.helper import extract_argument_group

from ament_tools.build_types.cmake_common import CMAKE_EXECUTABLE
from ament_tools.build_types.cmake_common import CTEST_EXECUTABLE
from ament_tools.build_types.cmake_common import cmakecache_exists_at
from ament_tools.build_types.cmake_common import get_visual_studio_version
from ament_tools.build_types.cmake_common import has_make_target
from ament_tools.build_types.cmake_common import MAKE_EXECUTABLE
from ament_tools.build_types.cmake_common import makefile_exists_at
from ament_tools.build_types.cmake_common import MSBUILD_EXECUTABLE
from ament_tools.build_types.cmake_common import NINJA_EXECUTABLE
from ament_tools.build_types.cmake_common import ninjabuild_exists_at
from ament_tools.build_types.cmake_common import project_file_exists_at
from ament_tools.build_types.cmake_common import solution_file_exists_at

from ament_tools.build_types.common import expand_package_level_setup_files
from ament_tools.build_types.common import get_cached_config
from ament_tools.build_types.common import set_cached_config

from ament_tools.verbs import VerbExecutionError

IS_WINDOWS = os.name == 'nt'


class CmakeBuildType(BuildType):
    build_type = 'cmake'
    description = "plain cmake project"

    def prepare_arguments(self, parser):
        parser.add_argument(
            '--force-cmake-configure',
            action='store_true',
            help="Invoke 'cmake' even if it has been executed before.")
        parser.add_argument(
            '--cmake-args',
            nargs='*',
            default=[],
            help="Arbitrary arguments which are passed to all CMake projects. "
                 "Argument collection can be terminated with '--'.")
        parser.add_argument(
            '--ctest-args',
            nargs='*',
            default=[],
            help="Arbitrary arguments which are passed to all CTest invocations. "
                 "The option is only used by the 'test*' verbs. "
                 "Argument collection can be terminated with '--'.")
        parser.add_argument(
            '--use-ninja',
            action='store_true',
            help="Invoke 'cmake' with '-G Ninja' and call ninja instead of make.")

    def argument_preprocessor(self, args):
        # The CMake pass-through flag collects dashed options.
        # This requires special handling or argparse will complain about
        # unrecognized options.
        args, cmake_args = extract_argument_group(args, '--cmake-args')
        args, ctest_args = extract_argument_group(args, '--ctest-args')
        extras = {
            'cmake_args': cmake_args,
            'ctest_args': ctest_args,
        }
        return args, extras

    def extend_context(self, options):
        ce = ContextExtender()
        force_cmake_configure = options.force_cmake_configure
        if getattr(options, 'force_configure', False):
            force_cmake_configure = True
        ce.add('force_cmake_configure', force_cmake_configure)
        ce.add('cmake_args', options.cmake_args)
        ce.add('ctest_args', options.ctest_args)
        ce.add('use_ninja', options.use_ninja)
        return ce

    def on_build(self, context):
        # Regardless of dry-run, try to determine if CMake should be invoked
        should_run_configure = False
        if context.force_cmake_configure:
            should_run_configure = True
        elif context.use_ninja and not ninjabuild_exists_at(context.build_space):
            should_run_configure = True
        elif not makefile_exists_at(context.build_space) or \
                not cmakecache_exists_at(context.build_space):
            # If either the Makefile or the CMake cache does not exist
            # we must configure
            should_run_configure = True
        cached_cmake_config = get_cached_config(
            context.build_space, 'cmake_args')
        cmake_config = {
            'cmake_args': context.cmake_args,
            'build_tests': context.build_tests,
            'install_space': context.install_space,
            'symlink_install': context.symlink_install,
        }
        if cmake_config != cached_cmake_config:
            should_run_configure = True
            self.warn("Running cmake because arguments have changed.")
        # Store the cmake_args for next invocation
        set_cached_config(context.build_space, 'cmake_args',
                          cmake_config)
        # Figure out if there is a setup file to source
        prefix = self._get_command_prefix('build', context)
        # Calculate any extra cmake args which are not common between cmake build types
        extra_cmake_args = []
        if should_run_configure:
            extra_cmake_args += context.cmake_args
        if context.use_ninja:
            extra_cmake_args += ['-G', 'Ninja']
        # Yield the cmake common on_build
        for step in self._common_cmake_on_build(
            should_run_configure, context, prefix, extra_cmake_args
        ):
            yield step

    def _common_cmake_on_build(self, should_run_configure, context, prefix, extra_cmake_args):
        # Execute the configure step
        # (either cmake or the cmake_check_build_system make target)
        if should_run_configure:
            cmake_args = [context.source_space]
            cmake_args.extend(extra_cmake_args)
            cmake_args += ["-DCMAKE_INSTALL_PREFIX=" + context.install_space]
            if IS_WINDOWS:
                vsv = get_visual_studio_version()
                if vsv is None:
                    sys.stderr.write(
                        'VisualStudioVersion is not set, '
                        'please run within a Visual Studio Command Prompt.\n')
                    raise VerbExecutionError('Could not determine Visual Studio Version')
                supported_vsv = {
                    '14.0': 'Visual Studio 14 2015 Win64',
                    '15.0': 'Visual Studio 15 2017 Win64',
                }
                if vsv not in supported_vsv:
                    raise VerbExecutionError('Unknown / unsupported VS version: ' + vsv)
                cmake_args += ['-G', supported_vsv[vsv]]
            if CMAKE_EXECUTABLE is None:
                raise VerbExecutionError("Could not find 'cmake' executable")
            yield BuildAction(prefix + [CMAKE_EXECUTABLE] + cmake_args)
        elif not IS_WINDOWS:  # Check for reconfigure if available.
            if MAKE_EXECUTABLE is None:
                raise VerbExecutionError("Could not find 'make' executable")
            cmd = prefix + [MAKE_EXECUTABLE, 'cmake_check_build_system']
            yield BuildAction(cmd)
        # Now execute the build step
        if not IS_WINDOWS:
            if context.use_ninja:
                if NINJA_EXECUTABLE is None:
                    raise VerbExecutionError("Could not find 'make' executable")
                yield BuildAction(prefix + [NINJA_EXECUTABLE] + context.make_flags)
            else:
                if MAKE_EXECUTABLE is None:
                    raise VerbExecutionError("Could not find 'make' executable")
                yield BuildAction(prefix + [MAKE_EXECUTABLE] + context.make_flags)
        else:
            if MSBUILD_EXECUTABLE is None:
                raise VerbExecutionError("Could not find 'msbuild' executable")
            solution_file = solution_file_exists_at(
                context.build_space, context.package_manifest.name)
            cmd = prefix + [MSBUILD_EXECUTABLE]
            env = None
            # Convert make parallelism flags into msbuild flags
            msbuild_flags = [
                x.replace('-j', '/m:') for x in context.make_flags if x.startswith('-j')
            ]
            if msbuild_flags:
                cmd += msbuild_flags
                # If there is a parallelism flag in msbuild_flags and it's not /m1,
                # then turn on /MP for the compiler (intra-project parallelism)
                if any([x.startswith('/m') for x in msbuild_flags]) and \
                   '/m:1' not in msbuild_flags:
                    env = dict(os.environ)
                    if 'CL' in env:
                        # make sure env['CL'] doesn't include an /MP already
                        if not any([x.startswith('/MP') for x in env['CL'].split(' ')]):
                            env['CL'] += ' /MP'
                    else:  # CL not in environment; let's add it with our flag
                        env['CL'] = '/MP'
            cmd += [
                '/p:Configuration=%s' %
                self._get_visual_studio_configuration(context), solution_file]
            yield BuildAction(cmd, env=env)

    def on_test(self, context):
        for step in self._common_cmake_on_test(context, 'cmake'):
            yield step

    def _common_cmake_on_test(self, context, build_type):
        assert context.build_tests
        # Figure out if there is a setup file to source
        # also pass the exec dependencies into the command prefix file
        prefix = self._get_command_prefix(
            'test', context,
            additional_dependencies=context.exec_dependency_paths_in_workspace
        )
        if not IS_WINDOWS:
            if has_make_target(context.build_space, 'test') or context.dry_run:
                if MAKE_EXECUTABLE is None:
                    raise VerbExecutionError("Could not find 'make' executable")
                cmd = prefix + [MAKE_EXECUTABLE, 'test']
                if 'ARGS' not in os.environ:
                    args = [
                        '-V',
                        # verbose output and generate xml of test summary
                        '-D', 'ExperimentalTest', '--no-compress-output']
                elif os.environ['ARGS']:
                    args = [os.environ['ARGS']]
                else:
                    args = []
                args += context.ctest_args
                if context.retest_until_pass and context.test_iteration:
                    args += ['--rerun-failed']
                if args:
                    # the valus is not quoted here
                    # since each item will be quoted by shlex.quote later if necessary
                    cmd.append('ARGS=%s' % ' '.join(args))
                yield BuildAction(cmd)
            else:
                self.warn("Could not run tests for '{0}' package because it has no "
                          "'test' target".format(build_type))
        else:
            if CTEST_EXECUTABLE is None:
                raise VerbExecutionError("Could not find 'ctest' executable")
            # invoke CTest directly in order to pass arguments
            # it needs a specific configuration and currently there are no conf. specific tests
            cmd = prefix + [
                CTEST_EXECUTABLE,
                # choose configuration on e.g. Windows
                '-C', self._get_visual_studio_configuration(context),
                # generate xml of test summary
                '-D', 'ExperimentalTest', '--no-compress-output',
                # show all test output
                '-V',
                '--force-new-ctest-process'] + \
                context.ctest_args
            if context.retest_until_pass and context.test_iteration:
                cmd += ['--rerun-failed']
            yield BuildAction(cmd)

    def _get_visual_studio_configuration(self, context):
        # check for CMake build type in the command line arguments
        arg_prefix = '-DCMAKE_BUILD_TYPE='
        build_type = None
        for cmake_arg in context.cmake_args:
            if cmake_arg.startswith(arg_prefix):
                build_type = cmake_arg[len(arg_prefix):]
                break
        else:
            # get for CMake build type from the CMake cache
            line_prefix = 'CMAKE_BUILD_TYPE:'
            cmake_cache = os.path.join(context.build_space, 'CMakeCache.txt')
            if os.path.exists(cmake_cache):
                with open(cmake_cache, 'r') as h:
                    lines = h.read().splitlines()
                for line in lines:
                    if line.startswith(line_prefix):
                        try:
                            index = line.index('=')
                        except ValueError:
                            continue
                        build_type = line[index + 1:]
                        break
        if build_type in ['Debug']:
            return 'Debug'
        return 'Release'

    def on_install(self, context):
        # First determine the files being deployed with skip_if_exists=True and remove them.
        environment_hooks_path = \
            os.path.join('share', context.package_manifest.name, 'environment')

        environment_hooks_to_be_deployed = []
        environment_hooks = []

        # Prepare to deploy AMENT_PREFIX_PATH environment hook
        ext = '.sh' if not IS_WINDOWS else '.bat'
        ament_prefix_path_template_path = get_environment_hook_template_path(
            'ament_prefix_path' + ext)
        environment_hooks_to_be_deployed.append(ament_prefix_path_template_path)
        environment_hooks.append(os.path.join(environment_hooks_path, 'ament_prefix_path' + ext))

        # Prepare to deploy PATH environment hook
        ext = '.sh' if not IS_WINDOWS else '.bat'
        path_template_path = get_environment_hook_template_path('path' + ext)
        environment_hooks_to_be_deployed.append(path_template_path)
        environment_hooks.append(os.path.join(environment_hooks_path, 'path' + ext))

        # Prepare to deploy library path environment hook if not on Windows
        if not IS_WINDOWS:
            library_template_path = get_environment_hook_template_path('library_path.sh')
            environment_hooks_to_be_deployed.append(library_template_path)
            environment_hooks.append(os.path.join(environment_hooks_path, 'library_path.sh'))

        # Expand package level setup files
        destinations = \
            expand_package_level_setup_files(context, environment_hooks, environment_hooks_path)

        # Remove package level setup files so they can be replaced correctly either in the
        # cmake install step or later with deploy_file(..., skip_if_exists=True)
        for destination in destinations:
            destination_path = compute_deploy_destination(
                context,
                os.path.basename(destination),
                os.path.dirname(os.path.relpath(destination, context.build_space))
            )
            if os.path.exists(destination_path) or os.path.islink(destination_path):
                os.remove(destination_path)

        # Call cmake common on_install (defined in CmakeBuildType)
        for step in self._common_cmake_on_install(context):
            yield step

        # Install files needed to extend the environment for build dependents to use this package
        # create marker file
        marker_file = os.path.join(
            context.install_space,
            'share', 'ament_index', 'resource_index', 'packages',
            context.package_manifest.name)
        if not os.path.exists(marker_file):
            marker_dir = os.path.dirname(marker_file)
            if not os.path.exists(marker_dir):
                os.makedirs(marker_dir)
            with open(marker_file, 'w'):  # "touching" the file
                pass

        # Deploy environment hooks
        for environment_hook in environment_hooks_to_be_deployed:
            deploy_file(
                context, os.path.dirname(environment_hook), os.path.basename(environment_hook),
                dst_subfolder=environment_hooks_path)

        # Expand package-level setup files
        for destination in destinations:
            deploy_file(
                context,
                os.path.dirname(destination), os.path.basename(destination),
                dst_subfolder=os.path.dirname(os.path.relpath(destination, context.build_space)),
                skip_if_exists=True)

    def _common_cmake_on_install(self, context):
        # Figure out if there is a setup file to source
        prefix = self._get_command_prefix('install', context)

        if not IS_WINDOWS:
            if context.use_ninja:
                if NINJA_EXECUTABLE is None:
                    raise VerbExecutionError("Could not find 'ninja' executable")
                yield BuildAction(prefix + [NINJA_EXECUTABLE, 'install'])
            else:
                if has_make_target(context.build_space, 'install') or context.dry_run:
                    if MAKE_EXECUTABLE is None:
                        raise VerbExecutionError("Could not find 'make' executable")
                    yield BuildAction(prefix + [MAKE_EXECUTABLE, 'install'])
                else:
                    self.warn('Could not run installation for package because it has no '
                              "'install' target")
        else:
            install_project_file = project_file_exists_at(context.build_space, 'INSTALL')
            if install_project_file is not None:
                if MSBUILD_EXECUTABLE is None:
                    raise VerbExecutionError("Could not find 'msbuild' executable")
                yield BuildAction(
                    prefix + [
                        MSBUILD_EXECUTABLE,
                        '/p:Configuration=' + self._get_visual_studio_configuration(context),
                        install_project_file])
            else:
                self.warn("Could not find Visual Studio project file 'INSTALL.vcxproj'")

    def on_uninstall(self, context):
        # Call cmake common on_uninstall (defined in CmakeBuildType)
        for step in self._common_cmake_on_uninstall(context, 'cmake'):
            yield step

    def _common_cmake_on_uninstall(self, context, build_type):
        # Figure out if there is a setup file to source
        prefix = self._get_command_prefix('uninstall', context)

        if not IS_WINDOWS:
            if has_make_target(context.build_space, 'uninstall'):
                if MAKE_EXECUTABLE is None:
                    raise VerbExecutionError("Could not find 'make' executable")
                cmd = prefix + [MAKE_EXECUTABLE, 'uninstall']
                yield BuildAction(cmd)
            else:
                self.warn("Could not run uninstall for '{0}' package because it has no "
                          "'uninstall' target".format(build_type))
        else:
            if MSBUILD_EXECUTABLE is None:
                raise VerbExecutionError("Could not find 'msbuild' executable")
            uninstall_project_file = project_file_exists_at(context.build_space, 'UNINSTALL')
            if uninstall_project_file is not None:
                yield BuildAction(prefix + [MSBUILD_EXECUTABLE, uninstall_project_file])
            else:
                self.warn("Could not find Visual Studio project file 'UNINSTALL.vcxproj'")

    def _get_command_prefix(self, name, context, additional_dependencies=None):
        if not IS_WINDOWS:
            return self._get_command_prefix_unix(name, context, additional_dependencies or [])
        else:
            return self._get_command_prefix_windows(name, context, additional_dependencies or [])

    def _get_command_prefix_windows(self, name, context, additional_dependencies):
        lines = []
        lines.append('@echo off\n')
        for path in context.build_dependencies + additional_dependencies:
            local_setup = os.path.join(path, 'local_setup.bat')
            lines.append(
                'if "%AMENT_TRACE_SETUP_FILES%" NEQ "" echo call "{0}"'.format(local_setup))
            lines.append('if exist "{0}" call "{0}"'.format(local_setup))
            lines.append('')
        lines.append(
            'set "CMAKE_PREFIX_PATH=%AMENT_PREFIX_PATH%;%CMAKE_PREFIX_PATH%"')
        lines.append('%*')
        lines.append('if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%')

        generated_file = os.path.join(
            context.build_space, '%s__%s.bat' %
            (CmakeBuildType.build_type, name))
        with open(generated_file, 'w') as h:
            for line in lines:
                h.write('%s\n' % line)

        return [generated_file]

    def _get_command_prefix_unix(self, name, context, additional_dependencies):
        lines = []
        lines.append('#!/usr/bin/env sh\n')
        for path in context.build_dependencies + additional_dependencies:
            local_setup = os.path.join(path, 'local_setup.sh')
            lines.append('if [ -n "$AMENT_TRACE_SETUP_FILES" ]; then')
            lines.append('  echo ". \\"%s\\""' % local_setup)
            lines.append('fi')
            lines.append('if [ -f "%s" ]; then' % local_setup)
            lines.append('  . "%s"' % local_setup)
            lines.append('fi')
            lines.append('')
        lines.append(
            'export CMAKE_PREFIX_PATH="$AMENT_PREFIX_PATH:$CMAKE_PREFIX_PATH"')

        generated_file = os.path.join(
            context.build_space, '%s__%s.sh' %
            (CmakeBuildType.build_type, name))
        with open(generated_file, 'w') as h:
            for line in lines:
                h.write('%s\n' % line)

        return ['.', generated_file, '&&']
