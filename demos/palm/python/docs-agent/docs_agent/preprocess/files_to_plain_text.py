#
# Copyright 2023 Google LLC
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
#

"""Process Markdown files into plain text"""

from markdown import markdown
import shutil
from bs4 import BeautifulSoup
import os
import re
import json
from absl import logging

from tqdm import tqdm
import frontmatter
from docs_agent.utilities import config
from docs_agent.utilities.config import ProductConfig, ReadConfig, Input
from docs_agent.models.tokenCount import returnHighestTokens
from docs_agent.utilities.helpers import (
    get_project_path,
    resolve_path,
    add_scheme_url,
    end_path_backslash,
    start_path_no_backslash,
)
from docs_agent.preprocess.splitters import markdown_splitter, html_splitter
from docs_agent.models import palm as palmModule
import uuid


# This function pre-processes files before they are actually chunked.
# This allows it to resolve includes of includes, Jinja templates, etc...
# TODO support Jinja, for this need to support data filters as well
# {% set doc | jsonloads %} and {% set teams | yamlloads %}
# Returns the temp_output which can then be deleted
def pre_process_doc_files(
    product_config: ProductConfig, inputpathitem: Input, temp_path: str
) -> str:
    temp_output = os.path.join(temp_path, product_config.output_path)
    # Delete directory if it exits, then create it.
    print(f"Temp output: {temp_output}")
    print("===========================================")
    if os.path.exists(temp_output):
        shutil.rmtree(temp_output)
        os.makedirs(temp_output)
    else:
        os.makedirs(temp_output)
    # Prepare progress bar
    file_count = sum(
        len(files) for _, _, files in os.walk(resolve_path(inputpathitem.path))
    )
    progress_bar = tqdm(
        total=file_count,
        position=0,
        bar_format="{percentage:3.0f}% | {n_fmt}/{total_fmt} | {elapsed}/{remaining}| {desc}",
    )
    for root, dirs, files in os.walk(resolve_path(inputpathitem.path)):
        if inputpathitem.exclude_path is not None:
            dirs[:] = [d for d in dirs if d not in inputpathitem.exclude_path]
        for file in files:
            # Displays status bar
            progress_bar.set_description_str(
                f"Pre-processing file {file}", refresh=True
            )
            progress_bar.update(1)
            # Process only Markdown files that do not begin with _(those should
            # be imported)
            # Construct a new sub-directory for storing output plain text files
            dir_path = os.path.join(
                temp_output, os.path.relpath(root, inputpathitem.path)
            )
            if not os.path.exists(dir_path):
                os.makedirs(dir_path)
            relative_path = make_relative_path(
                file=file, root=root, inputpath=inputpathitem.path
            )
            final_filename = temp_output + "/" + relative_path
            if file.startswith("_") and file.endswith(".md"):
                with open(os.path.join(root, file), "r", encoding="utf-8") as auto:
                    # Read the input Markdown content
                    content = auto.read()
                    auto.close()
                # Process includes lines in Markdown
                file_with_include = markdown_splitter.process_markdown_includes(
                    content, root
                )
                # Process include lines in HTML
                file_with_include = html_splitter.process_html_includes(
                    file_with_include, inputpathitem.include_path_html
                )
                with open(final_filename, "w", encoding="utf-8") as new_file:
                    new_file.write(content)
                    new_file.close()
            elif file.startswith("_") and (
                file.endswith(".html") or file.endswith(".htm")
            ):
                with open(os.path.join(root, file), "r", encoding="utf-8") as auto:
                    # Read the input HTML content
                    content = auto.read()
                    auto.close()
                with open(final_filename, "w", encoding="utf-8") as new_file:
                    new_file.write(content)
                    new_file.close()
            else:
                # Just copy files that that we don't need to preprocess
                # Such as images or files without underscores
                initial_file = os.path.join(root, file)
                # Errors with .gsheet, skip gsheet for now
                if not (file.endswith(".gsheet")):
                    shutil.copyfile(initial_file, final_filename)
    # Return the temporary directory, which can then be deleted once files are fully processed
    return temp_output


# This function processes files in the `input_path` directory
# into plain text files.
# Supports: Markdown files
# Includes are processed again since preprocess resolves the includes in
# Files prefixed with _ which indicates they are not standalone
# inputpath is optional to walk a temporary directory that has been pre-processed
# If not, defaults to path of inputpathitem
def process_files_from_input(
    product_config: ProductConfig,
    inputpathitem: Input,
    splitter: str,
    inputpath: str = None,
):
    # If inputpath isn't specified assign path from item
    if inputpath is None:
        inputpath = inputpathitem.path
    f_count = 0
    md_count = 0
    html_count = 0
    file_index = []
    full_file_metadata = {}
    resolved_output_path = resolve_path(product_config.output_path)
    # Pre-calculates file count
    file_count = sum(len(files) for _, _, files in os.walk(resolve_path(inputpath)))
    progress_bar = tqdm(
        total=file_count,
        position=0,
        bar_format="{percentage:3.0f}% | {n_fmt}/{total_fmt} | {elapsed}/{remaining}| {desc}",
    )
    for root, dirs, files in os.walk(resolve_path(inputpath)):
        if inputpathitem.exclude_path is not None:
            dirs[:] = [d for d in dirs if d not in inputpathitem.exclude_path]
        if inputpathitem.url_prefix is not None:
            # Makes sure that URL ends in backslash
            url_prefix = end_path_backslash(inputpathitem.url_prefix)
            namespace_uuid = uuid.uuid3(uuid.NAMESPACE_DNS, url_prefix)
        original_input = inputpathitem.path
        for file in files:
            # Displays status bar
            progress_bar.set_description_str(f"Processing file {file}", refresh=True)
            progress_bar.update(1)
            filename_to_open = os.path.join(root, file)
            # Construct a new sub-directory for storing output plain text files
            new_path = resolved_output_path + re.sub(
                resolve_path(inputpath), "", os.path.join(root, "")
            )
            is_exist = os.path.exists(new_path)
            if not is_exist:
                os.makedirs(new_path)
            relative_path = make_relative_path(
                file=file, root=root, inputpath=inputpath
            )
            if file.endswith(".md") and not file.startswith("_"):
                md_count += 1
                # Add filename to a list
                file_index.append(relative_path)
                with open(filename_to_open, "r", encoding="utf-8") as auto:
                    # Read the input Markdown content
                    to_file = auto.read()
                    auto.close()
                    # Process includes lines in Markdown
                    file_with_include = markdown_splitter.process_markdown_includes(
                        to_file, root
                    )
                    # Process include lines in HTML
                    file_with_include = html_splitter.process_html_includes(
                        file_with_include, inputpathitem.include_path_html
                    )
                    # This is an estimate of the token count
                    page_token_estimate = returnHighestTokens(file_with_include)
                    if splitter == "token_splitter":
                        # Returns an array of Section objects along with a Page
                        # Object that contains metadata
                        (
                            page_sections,
                            page,
                        ) = markdown_splitter.process_markdown_page(
                            markdown_text=file_with_include, header_id_spaces="-"
                        )
                        chunk_number = 0
                        for section in page_sections:
                            filename_to_save = make_chunk_name(
                                new_path=new_path,
                                file=file,
                                index=chunk_number,
                                extension="md",
                            )
                            # Generate UUID for each plain text chunk and collect its metadata,
                            # which will be written to the top-level `file_index.json` file.
                            md_hash = uuid.uuid3(namespace_uuid, section.content)
                            uuid_file = uuid.uuid3(namespace_uuid, filename_to_save)
                            origin_uuid = uuid.uuid3(namespace_uuid, relative_path)
                            # If no URL came from frontmatter, assign URL from config
                            if page.URL == "":
                                page.URL = end_path_backslash(
                                    add_scheme_url(url=url_prefix, scheme="https")
                                )
                            # Strip extension of .md from url
                            # Makes sure that relative_path starts without backslash
                            # page.url will have backslash
                            built_url = page.URL + start_path_no_backslash(
                                relative_path
                            )
                            strip_ext_url = re.search(r"(.*)\.md$", built_url)
                            built_url = strip_ext_url[1]
                            # Build the valid URL for a section including header
                            # Do not add a # if section 1
                            if section.name_id != "" and int(section.level) != 1:
                                built_url = built_url + "#" + section.name_id
                            # Adds additional info so that the section can know its origin
                            full_file_metadata[filename_to_save] = {
                                "UUID": str(uuid_file),
                                "origin_uuid": str(origin_uuid),
                                "source": str(original_input),
                                "source_file": str(relative_path),
                                "page_title": str(section.page_title),
                                "section_title": str(section.section_title),
                                "section_name_id": str(section.name_id),
                                "section_id": int(section.id),
                                "section_level": int(section.level),
                                "previous_id": int(section.previous_id),
                                "URL": str(built_url),
                                "md_hash": str(md_hash),
                                "token_estimate": float(section.token_count),
                                "full_token_estimate": float(page_token_estimate),
                                "parent_tree": list(section.parent_tree),
                                "metadata": dict(page.metadata),
                            }
                            with open(
                                filename_to_save, "w", encoding="utf-8"
                            ) as new_file:
                                new_file.write(section.content)
                                new_file.close()
                            chunk_number += 1
                    elif splitter == "process_sections":
                        # Use a custom splitter to split into small chunks
                        (
                            to_file,
                            metadata,
                        ) = markdown_splitter.process_page_and_section_titles(to_file)
                        to_file = markdown_splitter.process_markdown_includes(
                            to_file, root
                        )
                        docs = markdown_splitter.process_document_into_sections(to_file)
                        # doc = []
                        chunk_number = 0
                        for doc in docs:
                            filename_to_save = make_chunk_name(
                                new_path=new_path,
                                file=file,
                                index=chunk_number,
                                extension="md",
                            )
                            # Clean up Markdown and HTML syntax
                            content = markdown_splitter.markdown_to_text(doc)
                            # Generate UUID for each plain text chunk and collect its metadata,
                            # which will be written to the top-level `file_index.json` file.
                            md_hash = uuid.uuid3(namespace_uuid, file_with_include)
                            uuid_file = uuid.uuid3(namespace_uuid, filename_to_save)
                            full_file_metadata[filename_to_save] = {
                                "UUID": str(uuid_file),
                                "source": original_input,
                                "source_file": relative_path,
                                "URL": url_prefix,
                                "md_hash": str(md_hash),
                                "metadata": metadata,
                            }
                            with open(
                                filename_to_save, "w", encoding="utf-8"
                            ) as new_file:
                                new_file.write(content)
                                new_file.close()
                            chunk_number += 1
                        auto.close()
                    else:
                        # Exits if no valid markdown splitter
                        logging.error(
                            f"Select a valid markdown_splitter option in your configuration for {product_config.product_name}\n"
                        )
                        exit()
            elif (
                file.endswith(".htm") or file.endswith(".html")
            ) and not file.startswith("_"):
                html_count += 1
                # Add filename to a list
                file_index.append(relative_path)
                with open(filename_to_open, "r", encoding="utf-8") as auto:
                    # Read the input HTML content
                    to_file = auto.read()
                    # Process includes lines in HTML
                    file_with_include = html_splitter.process_html_includes(
                        to_file, inputpathitem.include_path_html
                    )
                    # print (to_file)
    # Counts actually processed files
    f_count = md_count + html_count
    print("\nProcessed " + str(f_count) + " files from the source: " + inputpath)
    print(str(md_count) + " Markdown files.")
    print(str(html_count) + " HTML files.")
    return f_count, md_count, html_count, file_index, full_file_metadata


# Write the recorded input variables into a file: `file_index.json`
def save_file_index_json(output_path, output_content):
    json_out_file = resolve_path(output_path) + "/file_index.json"
    with open(json_out_file, "w", encoding="utf-8") as outfile:
        json.dump(output_content, outfile)
    print(
        "Created " + json_out_file + " to store the complete list of processed files."
    )


# Given a file, root, and inputpath, make a relative path
def make_relative_path(file: str, inputpath: str, root: str = None) -> str:
    file_slash = "/" + file
    if root is None:
        relative_path = os.path.relpath(file_slash, inputpath)
    else:
        relative_path = os.path.relpath(root + file_slash, inputpath)
    return relative_path


# Given a path, file, chunk index, and an optional path extension (to save chunk)
# Create a chunk name
def make_chunk_name(new_path: str, file: str, index: int, extension: str = "md") -> str:
    # Grab the filename without the .md extension
    new_filename = os.path.join(new_path, file)
    match = re.search(r"(.*)\.md$", new_filename)
    new_filename_no_ext = match[1]
    # Save clean plain text to a new filename appended with an index
    filename_to_save = new_filename_no_ext + "_" + str(index) + "." + extension
    return filename_to_save


# Given a path, it resolves the path to an absolute path, and if it exists,
# deletes it, before re-creating it (essentially making a fresh directory)
# It then returns the absolute path name
def resolve_and_clear_path(path: str) -> str:
    resolved_output_path = resolve_path(path)
    # Remove the existing output, to make sure stale files are removed
    if os.path.exists(resolved_output_path):
        shutil.rmtree(resolved_output_path)
    os.makedirs(resolved_output_path, exist_ok=True)
    return resolved_output_path


# Processes all inputs from a given ProductConfig object
def process_inputs_from_product(input_product: ProductConfig, temp_process_path: str):
    source_file_index = {}
    total_file_count = 0
    total_md_count = 0
    total_html_count = 0
    final_file_metadata = {}
    for input_path_item in input_product.inputs:
        print(f"Input path: {input_path_item.path}")
        temp_output = pre_process_doc_files(
            product_config=input_product,
            inputpathitem=input_path_item,
            temp_path=temp_process_path,
        )
        # Process Markdown files in the `input` path, when using pre_proces_doc_files
        # temp_output should be used as inputpath parameter
        (
            file_count,
            md_count,
            html_count,
            file_index,
            full_file_metadata,
        ) = process_files_from_input(
            product_config=input_product,
            inputpathitem=input_path_item,
            inputpath=temp_output,
            splitter=input_product.markdown_splitter,
        )
        # Clear the temp_output
        shutil.rmtree(temp_output)
        input_path = input_path_item.path
        if not input_path.endswith("/"):
            input_path = input_path + "/"
        input_path = resolve_path(input_path)
        # Record the input variables used in this path.
        file_list = {}
        for file in file_index:
            file_obj = {file: {"source": input_path, "URL": input_path_item.url_prefix}}
            file_list[file] = file_obj
        # Make a single dictionary per product, append each input
        final_file_metadata = final_file_metadata | full_file_metadata
        # source_file_index[input_product.product_name] = full_file_metadata
        total_file_count += file_count
        total_md_count += md_count
        total_html_count += html_count
    source_file_index[input_product.product_name] = final_file_metadata
    # Write the recorded input variables into `file_index.json`.
    save_file_index_json(
        output_path=input_product.output_path, output_content=source_file_index
    )
    print(
        "==========================================="
        + f"\nFor product {input_product.product_name}:\n"
        + "Processed a total of "
        + str(total_file_count)
        + " files from "
        + str(len(input_product.inputs))
        + " sources.\n"
        + str(total_md_count)
        + " Markdown files.\n"
        + str(total_html_count)
        + " HTML files.\n"
    )


# Given a ReadConfig object, process all products
# Default Read config defaults to source of project with config.yaml
# temp_process_path is where temporary files will be processed and then deleted
# defaults to /tmp
def process_all_products(
    config_file: ReadConfig = config.ReadConfig().returnProducts(),
    temp_process_path: str = "/tmp",
):
    print(f"Starting chunker for {str(len(config_file.products))} products.\n")
    for index, product in enumerate(config_file.products):
        print(f"===========================================")
        print(f"Processing product: {product.product_name}")
        # logging.error(f"Index: {index}")
        # if index != 0:
        #     old_entries = read_file_index_json(output_path=input_product.output_path)
        #     logging.error(old_entries)
        # else:
        #     old_entries = None
        print("Output directory: " + resolve_and_clear_path(product.output_path))
        print("Processing files from " + str(len(product.inputs)) + " sources.")
        process_inputs_from_product(
            input_product=product, temp_process_path=temp_process_path
        )


def main():
    #### Main ####
    process_all_products()


if __name__ == "__main__":
    main()
