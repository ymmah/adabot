# The MIT License (MIT)
#
# Copyright (c) 2017 Scott Shawcroft for Adafruit Industries
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

from adabot import github_requests as github
import os
import subprocess
import shlex
from io import StringIO
from datetime import date

import sh
from sh.contrib import git

import redis
redis = redis.StrictRedis()

bundles = ["Adafruit_CircuitPython_Bundle", "CircuitPython_Community_Bundle"]

def fetch_bundle(bundle, bundle_path):
    if not os.path.isdir(bundle_path):
        os.makedirs(bundle_path, exist_ok=True)
        git.clone("-o", "adafruit", "https://github.com/adafruit/" + bundle + ".git", bundle_path)
    working_directory = os.getcwd()
    os.chdir(bundle_path)
    git.pull()
    git.submodule("init")
    git.submodule("update")
    os.chdir(working_directory)


class Submodule:
    def __init__(self, directory):
        self.directory = directory

    def __enter__(self):
        self.original_directory = os.path.abspath(os.getcwd())
        os.chdir(self.directory)

    def __exit__(self, exc_type, exc_value, traceback):
        os.chdir(self.original_directory)


def commit_to_tag(repo_path, commit):
    with Submodule(repo_path):
        try:
            output = StringIO()
            git.describe("--tags", "--exact-match", commit, _out=output)
            commit = output.getvalue().strip()
        except sh.ErrorReturnCode_128:
            pass
    return commit

def repo_version():
    version = StringIO()
    try:
        git.describe("--tags", "--exact-match", _out=version)
    except sh.ErrorReturnCode_128:
        git.log(pretty="format:%h", n=1, _out=version)

    return version.getvalue().strip()


def repo_sha():
    version = StringIO()
    git.log(pretty="format:%H", n=1, _out=version)
    return version.getvalue().strip()


def repo_remote_url(repo_path):
    with Submodule(repo_path):
        output = StringIO()
        git.remote("get-url", "origin", _out=output)
        return output.getvalue().strip()

def update_bundle(bundle_path):
    working_directory = os.path.abspath(os.getcwd())
    os.chdir(bundle_path)
    git.submodule("foreach", "git", "fetch")
    # sh fails to find the subcommand so we use subprocess.
    subprocess.run(shlex.split("git submodule foreach 'git checkout -q `git rev-list --tags --max-count=1`'"), stdout=subprocess.DEVNULL)

    # Don't update circuitpython, its going away soon.
    git.submodule("update", "circuitpython")

    status = StringIO()
    result = git.status("--short", _out=status)
    updates = []
    status = status.getvalue().strip()
    if status:
        for status_line in status.split("\n"):
            action, directory = status_line.split()
            if action != "M" or not directory.startswith("libraries"):
                RuntimeError("Unsupported updates")

            # Compute the tag difference.
            diff = StringIO()
            result = git.diff("--submodule=log", directory, _out=diff)
            diff_lines = diff.getvalue().split("\n")
            commit_range = diff_lines[0].split()[2]
            commit_range = commit_range.strip(":").split(".")
            old_commit = commit_to_tag(directory, commit_range[0])
            new_commit = commit_to_tag(directory, commit_range[-1])
            url = repo_remote_url(directory)
            summary = "\n".join(diff_lines[1:-1])
            updates.append((url[:-4], old_commit, new_commit, summary))
    os.chdir(working_directory)
    return updates

def commit_updates(bundle_path, update_info):
    working_directory = os.path.abspath(os.getcwd())
    message = ["Automated update by Adabot (adafruit/adabot@{})"
               .format(repo_version())]
    os.chdir(bundle_path)
    for url, old_commit, new_commit, summary in update_info:
        url_parts = url.split("/")
        user, repo = url_parts[-2:]
        summary = summary.replace("#", "{}/{}#".format(user, repo))
        message.append("Updating {} to {} from {}:\n{}".format(url,
                                                               new_commit,
                                                               old_commit,
                                                               summary))
    message = "\n\n".join(message)
    git.add(".")
    git.commit(message=message)
    os.chdir(working_directory)

def push_updates(bundle_path):
    working_directory = os.path.abspath(os.getcwd())
    os.chdir(bundle_path)
    git.push()
    os.chdir(working_directory)

def get_contributors(repo, commit_range):
    output = StringIO()
    git.log("--pretty=tformat:%H,%ae,%ce", commit_range, _out=output)
    output = output.getvalue().strip()
    contributors = {}
    if not output:
        return contributors
    for log_line in output.split("\n"):
        sha, author_email, committer_email = log_line.split(",")
        author = redis.get("github_username:" + author_email)
        committer = redis.get("github_username:" + committer_email)
        if not author or not committer:
            github_commit_info = github.get("/repos/" + repo + "/commits/" + sha)
            github_commit_info = github_commit_info.json()
            if github_commit_info["author"]:
                author = github_commit_info["author"]["login"]
                redis.set("github_username:" + author_email, author)
            if github_commit_info["committer"]:
                committer = github_commit_info["committer"]["login"]
                redis.set("github_username:" + committer_email, committer)
        else:
            author = author.decode("utf-8")
            committer = committer.decode("utf-8")

        if committer_email == "noreply@github.com":
            committer = None
        if author and author not in contributors:
            contributors[author] = 0
        if committer and committer not in contributors:
            contributors[committer] = 0
        if author:
            contributors[author] += 1
        if committer and committer != author:
            contributors[committer] += 1
    return contributors

def repo_name(url):
    # Strips off .git and splits on /
    url = url[:-4].split("/")
    return url[-2] + "/" + url[-1]

def add_contributors(master_list, additions):
    for contributor in additions:
        if contributor not in master_list:
            master_list[contributor] = 0
        master_list[contributor] += additions[contributor]

def new_release(bundle, bundle_path):
    working_directory = os.path.abspath(os.getcwd())
    os.chdir(bundle_path)
    print(bundle)
    current_release = github.get(
        "/repos/adafruit/{}/releases/latest".format(bundle))
    last_tag = current_release.json()["tag_name"]
    contributors = get_contributors("adafruit/" + bundle, last_tag + "..")
    added_submodules = []
    updated_submodules = []
    repo_links = {}

    output = StringIO()
    git.diff("--submodule=log", last_tag + "..", _out=output)
    output = output.getvalue().strip()
    if not output:
        print("Everything is already released.")
        return
    for line in output.split("\n"):
        if not line.startswith("Submodule"):
            continue
        line = line.split()
        directory = line[1]
        commit_range = line[2].strip(":")
        library_name = directory.split("/")[-1]
        if commit_range.startswith("0000000"):
            added_submodules.append(library_name)
            commit_range = commit_range.split(".")[-1]
        else:
            updated_submodules.append(library_name)

        repo_url = repo_remote_url(directory)

        new_commit = commit_range.split(".")[-1]
        release_tag = commit_to_tag(directory, new_commit)
        with Submodule(directory):
            submodule_contributors = get_contributors(repo_name(repo_url),
                                                      commit_range)
            add_contributors(contributors, submodule_contributors)
        repo_links[library_name] = repo_url[:-4] + "/releases/" + release_tag

    release_description = []
    if added_submodules:
        additions = []
        for library in added_submodules:
            additions.append("[{}]({})".format(library, repo_links[library]))
        release_description.append("New libraries: " + ", ".join(additions))

    if updated_submodules:
        updates = []
        for library in updated_submodules:
            updates.append("[{}]({})".format(library, repo_links[library]))
        release_description.append("Updated libraries: " + ", ".join(updates))

    release_description.append("")

    contributors = sorted(contributors, key=contributors.__getitem__, reverse=True)
    contributors = ["@" + x for x in contributors]

    release_description.append("As always, thank you to all of our contributors: " + ", ".join(contributors))

    release_description.append("\n--------------------------\n")

    release_description.append("The libraries in each release are compiled for all recent major versions of CircuitPython. Please download the one that matches your version of CircuitPython. For example, download the bundle with `2.x` in the filename for CircuitPython versions 2.0.0 and 2.1.0.\n")

    release_description.append("To install, simply download the matching zip file, unzip it, and copy the lib folder onto your CIRCUITPY drive. Non-express boards such as the [Trinket M0](https://www.adafruit.com/product/3500), [Gemma M0](https://www.adafruit.com/product/3501) and [Feather M0 Basic](https://www.adafruit.com/product/2772) will need to selectively copy files over.")

    release = {
        "tag_name": "{0:%Y%m%d}".format(date.today()),
        "target_commitish": repo_sha(),
        "name": "{0:%B} {0:%d}, {0:%Y} auto-release".format(date.today()),
        "body": "\n".join(release_description),
        "draft": False,
        "prerelease": False}

    response = github.post("/repos/adafruit/" + bundle + "/releases", json=release)
    if not response.ok:
        print(response.request.url)
        print(response.text)

    os.chdir(working_directory)

if __name__ == "__main__":
    directory = os.path.abspath(".bundles")
    for bundle in bundles:
        bundle_path = os.path.join(directory, bundle)
        fetch_bundle(bundle, bundle_path)
        update_info = update_bundle(bundle_path)
        if update_info:
            commit_updates(bundle_path, update_info)
            push_updates(bundle_path)
        new_release(bundle, bundle_path)
