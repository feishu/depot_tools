#!/usr/bin/env python
# Copyright 2017 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Splits a branch into smaller branches and uploads CLs."""

from __future__ import print_function

import collections
import os
import re
import subprocess2
import sys
import tempfile

import git_footers
import owners
import owners_finder

import git_common as git

import third_party.pygtrie as trie


# If a call to `git cl split` will generate more than this number of CLs, the
# command will prompt the user to make sure they know what they're doing. Large
# numbers of CLs generated by `git cl split` have caused infrastructure issues
# in the past.
CL_SPLIT_FORCE_LIMIT = 10


def ReadFile(file_path):
  """Returns the content of |file_path|."""
  with open(file_path) as f:
    content = f.read()
  return content


def EnsureInGitRepository():
  """Throws an exception if the current directory is not a git repository."""
  git.run('rev-parse')


def CreateBranchForDirectory(prefix, directory, upstream):
  """Creates a branch named |prefix| + "_" + |directory| + "_split".

  Return false if the branch already exists. |upstream| is used as upstream for
  the created branch.
  """
  existing_branches = set(git.branches(use_limit = False))
  branch_name = prefix + '_' + directory + '_split'
  if branch_name in existing_branches:
    return False
  git.run('checkout', '-t', upstream, '-b', branch_name)
  return True


def FormatDescriptionOrComment(txt, directory):
  """Replaces $directory with |directory| in |txt|."""
  return txt.replace('$directory', '/' + directory)


def AddUploadedByGitClSplitToDescription(description):
  """Adds a 'This CL was uploaded by git cl split.' line to |description|.

  The line is added before footers, or at the end of |description| if it has no
  footers.
  """
  split_footers = git_footers.split_footers(description)
  lines = split_footers[0]
  if not lines[-1] or lines[-1].isspace():
    lines = lines + ['']
  lines = lines + ['This CL was uploaded by git cl split.']
  if split_footers[1]:
    lines += [''] + split_footers[1]
  return '\n'.join(lines)


def UploadCl(refactor_branch, refactor_branch_upstream, directory, files,
             description, comment, reviewers, changelist, cmd_upload,
             cq_dry_run, enable_auto_submit):
  """Uploads a CL with all changes to |files| in |refactor_branch|.

  Args:
    refactor_branch: Name of the branch that contains the changes to upload.
    refactor_branch_upstream: Name of the upstream of |refactor_branch|.
    directory: Path to the directory that contains the OWNERS file for which
        to upload a CL.
    files: List of AffectedFile instances to include in the uploaded CL.
    description: Description of the uploaded CL.
    comment: Comment to post on the uploaded CL.
    reviewers: A set of reviewers for the CL.
    changelist: The Changelist class.
    cmd_upload: The function associated with the git cl upload command.
    cq_dry_run: If CL uploads should also do a cq dry run.
    enable_auto_submit: If CL uploads should also enable auto submit.
  """
  # Create a branch.
  if not CreateBranchForDirectory(
      refactor_branch, directory, refactor_branch_upstream):
    print('Skipping ' + directory + ' for which a branch already exists.')
    return

  # Checkout all changes to files in |files|.
  deleted_files = [f.AbsoluteLocalPath() for f in files if f.Action() == 'D']
  if deleted_files:
    git.run(*['rm'] + deleted_files)
  modified_files = [f.AbsoluteLocalPath() for f in files if f.Action() != 'D']
  if modified_files:
    git.run(*['checkout', refactor_branch, '--'] + modified_files)

  # Commit changes. The temporary file is created with delete=False so that it
  # can be deleted manually after git has read it rather than automatically
  # when it is closed.
  with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
    tmp_file.write(FormatDescriptionOrComment(description, directory))
    # Close the file to let git open it at the next line.
    tmp_file.close()
    git.run('commit', '-F', tmp_file.name)
    os.remove(tmp_file.name)

  # Upload a CL.
  upload_args = ['-f', '-r', ','.join(reviewers)]
  if cq_dry_run:
    upload_args.append('--cq-dry-run')
  if not comment:
    upload_args.append('--send-mail')
  if enable_auto_submit:
    upload_args.append('--enable-auto-submit')
  print('Uploading CL for ' + directory + '.')
  cmd_upload(upload_args)
  if comment:
    changelist().AddComment(FormatDescriptionOrComment(comment, directory),
                            publish=True)


class ClCandidate(object):

  def __init__(self, path, owners_db, author, files, owners=None):
    self._path = path
    self._files = files
    self._owners_db = owners_db
    self._author = author
    self._owners = owners

  def GetPath(self):
    return self._path

  def GetFiles(self):
    return self._files

  def AddFiles(self, files):
    self._owners = None
    self._files |= files

  def GetChangeSize(self):
    return sum([c[0] + c[1] for f in self._files for c in f.Changes()])

  def _EnsureOwners(self):
    if not self._owners:
      self._owners = set(
          self._owners_db.all_possible_owners(
              [f.LocalPath() for f in self._files], self._author).keys())

  def HaveCommonOwners(self, other):
    self._EnsureOwners()
    other._EnsureOwners()
    return len(self._owners & other._owners) > 0


def GetFilesSplitByOwners(owners_database, author, files, cl_size=500):
  """Returns a map of files split by OWNERS file.

  Returns:
    A map where keys are paths to directories containing an OWNERS file and
    values are lists of files sharing an OWNERS file.
  """
  candidates = trie.Trie()
  for f in files:
    path = f.LocalPath()
    candidates[path] = ClCandidate(path, owners_database, author, set([f]))

  change_lists = []

  edited = True
  while edited:
    edited = False
    for item in candidates.items():
      path = ''.join(item[0])
      candidate = item[1]

      sub_cls = len([''.join(k) for k in candidates.keys(path)]) - 1
      if not sub_cls:
        parent_path = os.path.dirname(path)
        if len(parent_path) > 0:
          if parent_path not in candidates:
            candidates[parent_path] = ClCandidate(
                parent_path, owners_database, author, set(),
                set(
                    owners_database.all_possible_owners([parent_path],
                                                        author).keys()))
          parent_cl = candidates[parent_path]

          if parent_cl.HaveCommonOwners(candidate):
            edited = True
            del candidates[path]
            parent_cl.AddFiles(candidate.GetFiles())

            if (parent_cl.GetChangeSize() +
                candidate.GetChangeSize()) >= cl_size:
              del candidates[parent_path]
              change_lists.append(parent_cl)

  for item in candidates.items():
    change_lists.append(item[1])

  files_split_by_owners = collections.defaultdict(list)
  for cl in change_lists:
    files_split_by_owners[cl.GetPath()] = cl.GetFiles()
  return files_split_by_owners


def PrintClInfo(cl_index, num_cls, directory, file_paths, description,
                reviewers):
  """Prints info about a CL.

  Args:
    cl_index: The index of this CL in the list of CLs to upload.
    num_cls: The total number of CLs that will be uploaded.
    directory: Path to the directory that contains the OWNERS file for which
        to upload a CL.
    file_paths: A list of files in this CL.
    description: The CL description.
    reviewers: A set of reviewers for this CL.
  """
  description_lines = FormatDescriptionOrComment(description,
                                                 directory).splitlines()
  indented_description = '\n'.join(['    ' + l for l in description_lines])

  print('CL {}/{}'.format(cl_index, num_cls))
  print('Path: {}'.format(directory))
  print('Reviewers: {}'.format(', '.join(reviewers)))
  print('\n' + indented_description + '\n')
  print('\n'.join(file_paths))
  print()


def SplitCl(description_file, comment_file, changelist, cmd_upload, dry_run,
            cq_dry_run, enable_auto_submit):
  """"Splits a branch into smaller branches and uploads CLs.

  Args:
    description_file: File containing the description of uploaded CLs.
    comment_file: File containing the comment of uploaded CLs.
    changelist: The Changelist class.
    cmd_upload: The function associated with the git cl upload command.
    dry_run: Whether this is a dry run (no branches or CLs created).
    cq_dry_run: If CL uploads should also do a cq dry run.
    enable_auto_submit: If CL uploads should also enable auto submit.

  Returns:
    0 in case of success. 1 in case of error.
  """
  description = AddUploadedByGitClSplitToDescription(ReadFile(description_file))
  comment = ReadFile(comment_file) if comment_file else None

  try:
    EnsureInGitRepository()

    cl = changelist()
    change = cl.GetChange(cl.GetCommonAncestorWithUpstream(), None)
    files = change.AffectedFiles()

    if not files:
      print('Cannot split an empty CL.')
      return 1

    author = git.run('config', 'user.email').strip() or None
    refactor_branch = git.current_branch()
    assert refactor_branch, "Can't run from detached branch."
    refactor_branch_upstream = git.upstream(refactor_branch)
    assert refactor_branch_upstream, \
        "Branch %s must have an upstream." % refactor_branch

    owners_database = owners.Database(change.RepositoryRoot(), file, os.path)
    owners_database.load_data_needed_for([f.LocalPath() for f in files])

    files_split_by_owners = GetFilesSplitByOwners(owners_database, author,
                                                  set(files))

    num_cls = len(files_split_by_owners)
    if cq_dry_run and num_cls > CL_SPLIT_FORCE_LIMIT:
      print(
        'This will generate "%r" CLs. This many CLs can potentially generate'
        ' too much load on the build infrastructure. Please email'
        ' infra-dev@chromium.org to ensure that this won\'t  break anything.'
        ' The infra team reserves the right to cancel your jobs if they are'
        ' overloading the CQ.' % num_cls)
      answer = raw_input('Proceed? (y/n):')
      if answer.lower() != 'y':
        return 0

    for cl_index, (directory, files) in \
        enumerate(files_split_by_owners.iteritems(), 1):
      # Use '/' as a path separator in the branch name and the CL description
      # and comment.
      directory = directory.replace(os.path.sep, '/')
      file_paths = [f.LocalPath() for f in files]
      reviewers = owners_database.reviewers_for(file_paths, author)

      if dry_run:
        PrintClInfo(cl_index, num_cls, directory, file_paths, description,
                    reviewers)
      else:
        UploadCl(refactor_branch, refactor_branch_upstream, directory, files,
                 description, comment, reviewers, changelist, cmd_upload,
                 cq_dry_run, enable_auto_submit)

    # Go back to the original branch.
    git.run('checkout', refactor_branch)

  except subprocess2.CalledProcessError as cpe:
    sys.stderr.write(cpe.stderr)
    return 1
  return 0
