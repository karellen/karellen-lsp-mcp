#   -*- coding: utf-8 -*-
#   Copyright 2026 Karellen, Inc.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

from pybuilder.core import (use_plugin, init, Author)

use_plugin("python.core")
use_plugin("python.unittest")
use_plugin("python.integrationtest")
use_plugin("python.flake8")
use_plugin("python.coverage")
use_plugin("python.coveralls")
use_plugin("python.distutils")

name = "karellen-lsp-mcp"
version = "0.1.5.dev"

summary = "MCP Server bridging to LSP servers for structured code introspection"
authors = [Author("Karellen, Inc.", "supervisor@karellen.co")]
maintainers = [Author("Arcadiy Ivanov", "arcadiy@karellen.co")]
url = "https://github.com/karellen/karellen-lsp-mcp"
urls = {
    "Bug Tracker": "https://github.com/karellen/karellen-lsp-mcp/issues",
    "Source Code": "https://github.com/karellen/karellen-lsp-mcp/",
}
license = "Apache-2.0"

requires_python = ">=3.10"

default_task = ["analyze", "publish"]


@init
def set_properties(project):
    project.depends_on("mcp")
    project.depends_on("lsprotocol")
    project.depends_on("filelock")
    project.depends_on("platformdirs")

    project.depends_on("karellen-clangd", extra="clangd")
    project.depends_on("karellen-jdtls", extra="jdtls")
    project.depends_on("karellen-clangd", extra="all")
    project.depends_on("karellen-jdtls", extra="all")

    project.set_property("integrationtest_inherit_environment", True)

    project.set_property("coverage_break_build", False)

    project.set_property("flake8_break_build", True)
    project.set_property("flake8_extend_ignore", "E303,E402")
    project.set_property("flake8_include_test_sources", True)
    project.set_property("flake8_include_scripts", True)
    project.set_property("flake8_max_line_length", 130)

    project.set_property("distutils_readme_description", True)
    project.set_property("distutils_description_overwrite", True)
    project.set_property("distutils_upload_skip_existing", True)
    project.set_property("distutils_console_scripts", ["karellen-lsp-mcp = karellen_lsp_mcp.server:main"])
    project.set_property("distutils_setup_keywords", ["lsp", "language-server-protocol", "mcp",
                                                       "model-context-protocol", "clangd",
                                                       "code-intelligence"])

    project.set_property("distutils_classifiers", [
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
        "Operating System :: POSIX :: Linux",
        "Operating System :: MacOS",
        "Environment :: Console",
        "Topic :: Software Development",
        "Intended Audience :: Developers",
        "Development Status :: 3 - Alpha"
    ])
