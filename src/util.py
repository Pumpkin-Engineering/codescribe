# REMEMBER: this is python 2.7
import io
import os
import shutil
import sys
import traceback

import scriptengine  # type: ignore

from object_type import get_object_type

EXPORT_STAGING_SUFFIX = ".codescribe_staging"
EXPORT_BACKUP_SUFFIX = ".codescribe_backup"

# Line ending written into every text file CODESCRIBE exports. CODESYS itself
# emits CRLF and the reference exports are pinned to CRLF, so that stays the
# default and out-of-the-box behaviour is unchanged. Call set_line_ending("\n")
# before an export to produce LF instead (native xml included). Reads are
# unaffected: open_utf8 always reads with universal newlines, so imports stay
# line-ending agnostic whichever way the files were written.
CRLF = "\r\n"
CR = "\r"
LF = "\n"
_LINE_ENDING = CRLF


def set_line_ending(ending):
    global _LINE_ENDING
    _LINE_ENDING = ending


def get_line_ending():
    return _LINE_ENDING


# How a completed export is landed on the target folder. The default swaps the
# staging folder into place (an atomic rename), which is what CODESCRIBE has
# always done. Call set_export_inplace(True) to instead write the staged files
# into the existing target without ever renaming it - so a target folder, or an
# individual exported file, that is a symlink into the place the sources are
# really versioned survives the export. An in-place landing also prunes the
# folders the export no longer uses (a swap gets this for free by replacing the
# whole folder).
#
# Opting in explicitly is the reliable control. A symlinked target is also
# landed in place automatically, but that detection (os.path.islink) is only
# best-effort: over a VirtualBox shared folder or other network share the guest
# may not report a symlink as one, so never rely on it - opt in.
EXPORT_IN_PLACE = False


def set_export_inplace(enabled):
    global EXPORT_IN_PLACE
    EXPORT_IN_PLACE = enabled


def get_export_inplace():
    return EXPORT_IN_PLACE


class ExportFolderLockedError(EnvironmentError):
    """The target export folder could not be replaced. The completed export is
    preserved in the staging folder."""


class NothingExportedError(EnvironmentError):
    """The export produced no files at all, so the existing export folder was
    left untouched rather than being replaced with an empty one."""


def begin_export_folder(target_folder):
    # The export is written into a sibling staging folder and only swapped into
    # place once it completes, so a locked target folder or a mid-export crash
    # cannot destroy the existing on-disk copy.
    staging_folder = target_folder + EXPORT_STAGING_SUFFIX
    if os.path.exists(staging_folder):
        shutil.rmtree(staging_folder)
    os.mkdir(staging_folder)
    return staging_folder


def _remove_stale_files(staging_folder, target_folder):
    """Delete target files this export did not write. Returns those that stay.

    The swap path replaces the folder wholesale, so anything the export did
    not produce is gone by definition. The sync path has to do the same, or a
    rendering from a previous export is left beside a new native xml and
    describes a POU that has since changed - with nothing to say so. Renaming
    a folder is what a lock stops; deleting a file inside it usually still
    works, and where it does not the file is named rather than left silent.
    """
    stale = []
    for dir_path, _dir_names, file_names in os.walk(target_folder):
        relative = os.path.relpath(dir_path, target_folder)
        source = staging_folder if relative == "." else os.path.join(staging_folder, relative)
        for file_name in file_names:
            if os.path.exists(os.path.join(source, file_name)):
                continue
            try:
                os.remove(os.path.join(dir_path, file_name))
            except (OSError, IOError):
                stale.append(os.path.join(dir_path, file_name))
    return stale


def _sync_export_files(staging_folder, target_folder):
    # Overwrite sync; anything in the target the export did not write is
    # removed afterwards by _remove_stale_files.
    for dir_path, dir_names, file_names in os.walk(staging_folder):
        relative = os.path.relpath(dir_path, staging_folder)
        destination = target_folder if relative == "." else os.path.join(target_folder, relative)
        # isdir follows symlinks, so a folder that is a symlink passes and is
        # reused rather than replaced.
        if not os.path.isdir(destination):
            os.makedirs(destination)
        for file_name in file_names:
            # copyfile (not copy2) writes the bytes only: it opens the
            # destination path, so an existing file symlink is written through
            # and kept, and it preserves the file's inode. It deliberately skips
            # metadata, because chmod/utime (what copy2 would apply) commonly
            # fails over a VirtualBox shared folder, and rewriting the mtime of
            # an unchanged file is churn on anything watching the folder.
            shutil.copyfile(os.path.join(dir_path, file_name), os.path.join(destination, file_name))


def prune_empty_dirs(root):
    # Remove directories the export no longer produced, deepest first so a
    # parent emptied by pruning a child is caught in the same pass. The root and
    # anything that is a symlink are never removed - rmdir on a directory symlink
    # deletes the link, which is the very thing an in-place export preserves.
    pruned = []
    all_dirs = []
    for dir_path, _dir_names, _file_names in os.walk(root):
        all_dirs.append(dir_path)
    for dir_path in sorted(all_dirs, key=len, reverse=True):
        if dir_path == root or os.path.islink(dir_path):
            continue
        try:
            if not os.listdir(dir_path):
                os.rmdir(dir_path)
                pruned.append(dir_path)
        except (OSError, IOError):
            pass
    return pruned


def _land_in_place(staging_folder, target_folder):
    # Copy the staged files into the existing target by content, drop the files
    # the export no longer produces and the folders that then became empty, and
    # remove the staging folder. Returns the stale files it could not delete.
    _sync_export_files(staging_folder, target_folder)
    stale = _remove_stale_files(staging_folder, target_folder)
    shutil.rmtree(staging_folder)
    prune_empty_dirs(target_folder)
    return stale


def _folder_has_files(folder):
    for _dir_path, _dir_names, file_names in os.walk(folder):
        if file_names:
            return True
    return False


def finalize_export_folder(target_folder, staging_folder, in_place=None):
    # An export that wrote no files must never replace the previous export:
    # swapping in an empty staging folder silently destroys it. Seen when
    # Export Lib To Files runs on a device project - its walker only exports
    # objects directly under the project root, and a device project keeps
    # everything under Devices, so the walk produces nothing.
    if not _folder_has_files(staging_folder):
        shutil.rmtree(staging_folder)
        raise NothingExportedError(
            "Nothing was exported, so the existing export folder was left untouched: "
            + target_folder
            + ". Export To Files needs a device project (objects under a Device); Export Lib To Files"
            + " needs a library project (objects directly under the project root)."
        )

    if in_place is None:
        in_place = EXPORT_IN_PLACE
    # Best-effort: a symlinked target is landed in place because renaming it would
    # move the link. Over a network share islink may not report a symlink, so this
    # is only a safety net - callers there must opt in explicitly (EXPORT_IN_PLACE).
    if os.path.islink(target_folder):
        in_place = True

    if in_place:
        try:
            stale = _land_in_place(staging_folder, target_folder)
        except (OSError, IOError) as sync_error:
            raise ExportFolderLockedError(
                "Could not update the export folder "
                + target_folder
                + " in place. It is likely locked by another program (an open Explorer window, IDE, git client or antivirus)."
                + " The completed export is preserved in "
                + staging_folder
                + ". Original error: "
                + str(sync_error)
            )
        if stale:
            print(
                "WARNING: "
                + str(len(stale))
                + " file(s) this export did not write could not be removed and are now stale: "
                + ", ".join(stale[:6])
            )
        return

    backup_folder = target_folder + EXPORT_BACKUP_SUFFIX
    try:
        if os.path.exists(target_folder):
            if os.path.exists(backup_folder):
                shutil.rmtree(backup_folder)
            os.rename(target_folder, backup_folder)
        os.rename(staging_folder, target_folder)
    except (OSError, IOError) as rename_error:
        # On Windows these renames fail while another program holds a handle on
        # the target folder. Fall back to copying the staged files into it.
        try:
            stale = _land_in_place(staging_folder, target_folder)
        except (OSError, IOError):
            raise ExportFolderLockedError(
                "Could not replace the export folder "
                + target_folder
                + ". It is likely locked by another program (an open Explorer window, IDE, git client or antivirus)."
                + " The completed export is preserved in "
                + staging_folder
                + ". Original error: "
                + str(rename_error)
            )
        print(
            "Export folder "
            + target_folder
            + " is in use; synced the staged files into it instead of swapping folders."
        )
        if stale:
            print(
                "WARNING: "
                + str(len(stale))
                + " file(s) this export did not write could not be removed and are now stale: "
                + ", ".join(stale[:6])
            )
        return
    try:
        if os.path.exists(backup_folder):
            shutil.rmtree(backup_folder)
    except (OSError, IOError) as backup_error:
        # The export itself succeeded at this point; a leftover backup folder is
        # not worth failing it over.
        print("Warning: could not delete backup folder " + backup_folder + ": " + str(backup_error))


def ui_info(message):
    # Blocking dialog; the message view is easy to miss, so entry scripts report
    # their outcome through these as well.
    scriptengine.system.ui.info(message)


def ui_error_with_traceback(message):
    scriptengine.system.ui.error(message + "\n\n" + traceback.format_exc())


def open_utf8(path, mode):
    # ScriptEngine runs on Python 2.7; the builtin open() writes a byte stream and would
    # implicitly encode unicode text as ascii, crashing on Cyrillic / accented chars / smart
    # punctuation. io.open with an explicit encoding keeps everything UTF-8 round-trippable.
    # Writing honours the configured line ending; reading always uses universal newlines
    # (newline=None) so a file is imported correctly whatever endings it was written with.
    newline = _LINE_ENDING if ("w" in mode or "a" in mode) else None
    return io.open(path, mode, encoding="utf-8", newline=newline)


def print_python_version():
    print("Python version: " + sys.version)


def assert_project_open():
    if scriptengine.projects.primary is None:
        raise ValueError("You must have a project open!")


def assert_path_exists(path):
    if not os.path.exists(path):
        raise ValueError("Path " + path + " does not exist")


def first_or_none(lst):
    return next(iter(lst), None)


def first_of_type_or_error(lst, obj_type, err):
    for obj in lst:
        if get_object_type(obj) == obj_type:
            return obj
    raise ValueError(err)


def first_of_type_or_none(lst, obj_type):
    for obj in lst:
        if get_object_type(obj) == obj_type:
            return obj
    return None


def first_or_error(lst, err):
    try:
        return next(iter(lst))
    except StopIteration:
        raise ValueError(err)
