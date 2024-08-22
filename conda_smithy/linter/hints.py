import os
import re
import shutil
import subprocess
import sys
from glob import glob

from conda_smithy.linter import conda_recipe_v1_linter
from conda_smithy.linter.errors import HINT_NO_ARCH
from conda_smithy.linter.utils import (
    VALID_PYTHON_BUILD_BACKENDS,
    find_local_config_file,
    is_selector_line,
)
from conda_smithy.utils import get_yaml


def hint_pip_usage(build_section, hints):
    if "script" in build_section:
        scripts = build_section["script"]
        if isinstance(scripts, str):
            scripts = [scripts]
        for script in scripts:
            if "python setup.py install" in script:
                hints.append(
                    "Whenever possible python packages should use pip. "
                    "See https://conda-forge.org/docs/maintainer/adding_pkgs.html#use-pip"
                )


def hint_suggest_noarch(
    noarch_value,
    build_reqs,
    raw_requirements_section,
    is_staged_recipes,
    conda_forge,
    recipe_fname,
    hints,
    recipe_version: int = 0,
):
    if (
        noarch_value is None
        and build_reqs
        and not any(["_compiler_stub" in b for b in build_reqs])
        and ("pip" in build_reqs)
        and (is_staged_recipes or not conda_forge)
    ):
        if recipe_version == 1:
            conda_recipe_v1_linter.hint_noarch_usage(
                build_reqs, raw_requirements_section, hints
            )
        else:
            with open(recipe_fname) as fh:
                in_runreqs = False
                no_arch_possible = True
                for line in fh:
                    line_s = line.strip()
                    if line_s == "host:" or line_s == "run:":
                        in_runreqs = True
                        runreqs_spacing = line[: -len(line.lstrip())]
                        continue
                    if line_s.startswith("skip:") and is_selector_line(line):
                        no_arch_possible = False
                        break
                    if in_runreqs:
                        if runreqs_spacing == line[: -len(line.lstrip())]:
                            in_runreqs = False
                            continue
                        if is_selector_line(line):
                            no_arch_possible = False
                            break
                if no_arch_possible:
                    hints.append(HINT_NO_ARCH)


def hint_shellcheck_usage(recipe_dir, hints):
    shellcheck_enabled = False
    shell_scripts = []
    if recipe_dir:
        shell_scripts = glob(os.path.join(recipe_dir, "*.sh"))
        forge_yaml = find_local_config_file(recipe_dir, "conda-forge.yml")
        if shell_scripts and forge_yaml:
            with open(forge_yaml) as fh:
                code = get_yaml().load(fh)
                shellcheck_enabled = code.get("shellcheck", {}).get(
                    "enabled", shellcheck_enabled
                )

        if shellcheck_enabled and shutil.which("shellcheck") and shell_scripts:
            max_shellcheck_lines = 50
            cmd = [
                "shellcheck",
                "--enable=all",
                "--shell=bash",
                # SC2154: var is referenced but not assigned,
                #         see https://github.com/koalaman/shellcheck/wiki/SC2154
                "--exclude=SC2154",
            ]

            p = subprocess.Popen(
                cmd + shell_scripts,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env={
                    "PATH": os.getenv("PATH")
                },  # exclude other env variables to protect against token leakage
            )
            sc_stdout, _ = p.communicate()

            if p.returncode == 1:
                # All files successfully scanned with some issues.
                findings = (
                    sc_stdout.decode(sys.stdout.encoding)
                    .replace("\r\n", "\n")
                    .splitlines()
                )
                hints.append(
                    "Whenever possible fix all shellcheck findings ('"
                    + " ".join(cmd)
                    + " recipe/*.sh -f diff | git apply' helps)"
                )
                hints.extend(findings[:50])
                if len(findings) > max_shellcheck_lines:
                    hints.append(
                        "Output restricted, there are '%s' more lines."
                        % (len(findings) - max_shellcheck_lines)
                    )
            elif p.returncode != 0:
                # Something went wrong.
                hints.append(
                    "There have been errors while scanning with shellcheck."
                )


def hint_check_spdx(about_section, hints):
    import license_expression

    license = about_section.get("license", "")
    licensing = license_expression.Licensing()
    parsed_exceptions = []
    try:
        parsed_licenses = []
        parsed_licenses_with_exception = licensing.license_symbols(
            license.strip(), decompose=False
        )
        for li in parsed_licenses_with_exception:
            if isinstance(li, license_expression.LicenseWithExceptionSymbol):
                parsed_licenses.append(li.license_symbol.key)
                parsed_exceptions.append(li.exception_symbol.key)
            else:
                parsed_licenses.append(li.key)
    except license_expression.ExpressionError:
        parsed_licenses = [license]

    licenseref_regex = re.compile(r"^LicenseRef[a-zA-Z0-9\-.]*$")
    filtered_licenses = []
    for license in parsed_licenses:
        if not licenseref_regex.match(license):
            filtered_licenses.append(license)

    with open(os.path.join(os.path.dirname(__file__), "licenses.txt")) as f:
        expected_licenses = f.readlines()
        expected_licenses = set([li.strip() for li in expected_licenses])
    with open(
        os.path.join(os.path.dirname(__file__), "license_exceptions.txt")
    ) as f:
        expected_exceptions = f.readlines()
        expected_exceptions = set([li.strip() for li in expected_exceptions])
    if set(filtered_licenses) - expected_licenses:
        hints.append(
            "License is not an SPDX identifier (or a custom LicenseRef) nor an SPDX license expression.\n\n"
            "Documentation on acceptable licenses can be found "
            "[here]( https://conda-forge.org/docs/maintainer/adding_pkgs.html#spdx-identifiers-and-expressions )."
        )
    if set(parsed_exceptions) - expected_exceptions:
        hints.append(
            "License exception is not an SPDX exception.\n\n"
            "Documentation on acceptable licenses can be found "
            "[here]( https://conda-forge.org/docs/maintainer/adding_pkgs.html#spdx-identifiers-and-expressions )."
        )


def hint_pip_no_build_backend(host_or_build_section, package_name, hints):
    if host_or_build_section and any(
        req.split(" ")[0] == "pip" for req in host_or_build_section
    ):
        found_backend = False
        for backend in VALID_PYTHON_BUILD_BACKENDS:
            if any(
                req.split(" ")[0] in [backend, backend.replace("-", "_"), backend.replace("_", "-")] for req in host_or_build_section
            ):
                found_backend = True
                break

        if not found_backend:
            hints.append(
                f"No valid build backend found for Python recipe for package `{package_name}` using `pip`. Python recipes using `pip` need to "
                "explicitly specify a build backend in the `host` section. "
                "If your recipe has built with only `pip` in the `host` section in the past, you likely should "
                "add `setuptools` to the `host` section of your recipe."
            )
