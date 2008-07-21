#
# fs.py : Filesystem related utilities and classes
#
# Copyright 2007, Red Hat  Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 2 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Library General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.

import os
import os.path
import sys
import errno
import stat
import subprocess
import random
import string
import logging

from imgcreate.errors import *

def makedirs(dirname):
    """A version of os.makedirs() that doesn't throw an
    exception if the leaf directory already exists.
    """
    try:
        os.makedirs(dirname)
    except OSError, (err, msg):
        if err != errno.EEXIST:
            raise

def mksquashfs(in_img, out_img):
    args = ["/sbin/mksquashfs", in_img, out_img]

    if not sys.stdout.isatty():
        args.append("-no-progress")

    ret = subprocess.call(args)
    if ret != 0:
        raise SquashfsError("'%s' exited with error (%d)" %
                            (string.join(args, " "), ret))

def resize2fs(fs, size):
    dev_null = os.open("/dev/null", os.O_WRONLY)
    try:
        return subprocess.call(["/sbin/resize2fs", fs, "%sK" % (size / 1024,)],
                               stdout = dev_null, stderr = dev_null)
    finally:
        os.close(dev_null)

class BindChrootMount:
    """Represents a bind mount of a directory into a chroot."""
    def __init__(self, src, chroot, dest = None):
        self.src = src
        self.root = chroot

        if not dest:
            dest = src
        self.dest = self.root + "/" + dest

        self.mounted = False

    def mount(self):
        if self.mounted:
            return

        makedirs(self.dest)
        rc = subprocess.call(["/bin/mount", "--bind", self.src, self.dest])
        if rc != 0:
            raise MountError("Bind-mounting '%s' to '%s' failed" %
                             (self.src, self.dest))
        self.mounted = True

    def unmount(self):
        if not self.mounted:
            return

        subprocess.call(["/bin/umount", self.dest])
        self.mounted = False

class LoopbackMount:
    def __init__(self, lofile, mountdir, fstype = None):
        self.lofile = lofile
        self.mountdir = mountdir
        self.fstype = fstype

        self.mounted = False
        self.losetup = False
        self.rmdir   = False
        self.loopdev = None

    def cleanup(self):
        self.unmount()
        self.lounsetup()

    def unmount(self):
        if self.mounted:
            rc = subprocess.call(["/bin/umount", self.mountdir])
            if rc == 0:
                self.mounted = False

        if self.rmdir and not self.mounted:
            try:
                os.rmdir(self.mountdir)
            except OSError, e:
                pass
            self.rmdir = False

    def lounsetup(self):
        if self.losetup:
            rc = subprocess.call(["/sbin/losetup", "-d", self.loopdev])
            self.losetup = False
            self.loopdev = None

    def loopsetup(self):
        if self.losetup:
            return

        losetupProc = subprocess.Popen(["/sbin/losetup", "-f"],
                                       stdout=subprocess.PIPE)
        losetupOutput = losetupProc.communicate()[0]

        if losetupProc.returncode:
            raise MountError("Failed to allocate loop device for '%s'" %
                             self.lofile)

        self.loopdev = losetupOutput.split()[0]

        rc = subprocess.call(["/sbin/losetup", self.loopdev, self.lofile])
        if rc != 0:
            raise MountError("Failed to allocate loop device for '%s'" %
                             self.lofile)

        self.losetup = True

    def mount(self):
        if self.mounted:
            return

        self.loopsetup()

        if not os.path.isdir(self.mountdir):
            os.makedirs(self.mountdir)
            self.rmdir = True

        args = [ "/bin/mount", self.loopdev, self.mountdir ]
        if self.fstype:
            args.extend(["-t", self.fstype])

        rc = subprocess.call(args)
        if rc != 0:
            raise MountError("Failed to mount '%s' to '%s'" %
                             (self.loopdev, self.mountdir))

        self.mounted = True

class SparseLoopbackMount(LoopbackMount):
    def __init__(self, lofile, mountdir, size, fstype = None):
        LoopbackMount.__init__(self, lofile, mountdir, fstype)
        self.size = size

    def expand(self, create = False, size = None):
        flags = os.O_WRONLY
        if create:
            flags |= os.O_CREAT
            makedirs(os.path.dirname(self.lofile))

        if size is None:
            size = self.size

        fd = os.open(self.lofile, flags)

        os.lseek(fd, size, 0)
        os.write(fd, '\x00')
        os.close(fd)

    def truncate(self, size = None):
        if size is None:
            size = self.size
        fd = os.open(self.lofile, os.O_WRONLY)
        os.ftruncate(fd, size)
        os.close(fd)

    def create(self):
        self.expand(create = True)

class SparseExtLoopbackMount(SparseLoopbackMount):
    def __init__(self, lofile, mountdir, size, fstype, blocksize, fslabel):
        SparseLoopbackMount.__init__(self, lofile, mountdir, size, fstype)
        self.blocksize = blocksize
        self.fslabel = fslabel

    def __format_filesystem(self):
        rc = subprocess.call(["/sbin/mkfs." + self.fstype,
                              "-F", "-L", self.fslabel,
                              "-m", "1", "-b", str(self.blocksize),
                              self.lofile,
                              str(self.size / self.blocksize)])
        if rc != 0:
            raise MountError("Error creating %s filesystem" % (self.fstype,))
        subprocess.call(["/sbin/tune2fs", "-c0", "-i0", "-Odir_index",
                         "-ouser_xattr,acl", self.lofile])

    def create(self):
        SparseLoopbackMount.create(self)
        self.__format_filesystem()

    def resize(self, size = None):
        current_size = os.stat(self.lofile)[stat.ST_SIZE]

        if size is None:
            size = self.size

        if size == current_size:
            return

        if size > current_size:
            self.expand(size)

        self.__fsck()

        resize2fs(self.lofile, size)

        if size < current_size:
            self.truncate(size)
        return size

    def mount(self):
        if not os.path.isfile(self.lofile):
            self.create()
        else:
            self.resize()
        return SparseLoopbackMount.mount(self)

    def __fsck(self):
        subprocess.call(["/sbin/e2fsck", "-f", "-y", self.lofile])

    def __get_size_from_filesystem(self):
        def parse_field(output, field):
            for line in output.split("\n"):
                if line.startswith(field + ":"):
                    return line[len(field) + 1:].strip()

            raise KeyError("Failed to find field '%s' in output" % field)

        dev_null = os.open("/dev/null", os.O_WRONLY)
        try:
            out = subprocess.Popen(['/sbin/dumpe2fs', '-h', self.lofile],
                                   stdout = subprocess.PIPE,
                                   stderr = dev_null).communicate()[0]
        finally:
            os.close(dev_null)

        return int(parse_field(out, "Block count")) * self.blocksize

    def __resize_to_minimal(self):
        self.__fsck()

        #
        # Use a binary search to find the minimal size
        # we can resize the image to
        #
        bot = 0
        top = self.__get_size_from_filesystem()
        while top != (bot + 1):
            t = bot + ((top - bot) / 2)

            if not resize2fs(self.lofile, t):
                top = t
            else:
                bot = t
        return top

    def resparse(self, size = None):
        self.cleanup()
        
        minsize = self.__resize_to_minimal()

        self.truncate(minsize)
        self.resize(size)
        return minsize

class Disk:
    """
    With the new disk API the image being produced can be partitioned into
    multiple chunks, with many filesystems.  Furthermore, not all of them
    require loopback mounts, as the partitions themselves are already
    visible via /dev/mapper/

    There are now classes which deal with accessing / creating disks:

      Disk               - generic base for disks
      RawDisk            - a disk backed by a block device
      LoopbackDisk       - a disk backed by a file
      SparseLoopbackDisk - a disk backed by a sparse file

    The 'create' method must make the disk visible as a block device - eg
    by calling losetup. For RawDisk, this is obviously a no-op. The 'cleanup'
    method must undo the 'create' operation.

    There are then classes which deal with mounting things:

       Mount        - generic base for mounts
       DiskMount    -  able to mount a Disk object
       ExtDiskMount - able to format/resize ext3 filesystems when mounting
    """
    def __init__(self, size, device = None):
        self._device = device
        self._size = size

    def create(self):
        pass

    def cleanup(self):
        pass

    def get_device(self):
        return self._device
    def set_device(self, path):
        self._device = path
    device = property(get_device, set_device)

    def get_size(self):
        return self._size
    size = property(get_size)


class RawDisk(Disk):
    def __init__(self, size, device):
        Disk.__init__(self, size, device)

    def fixed(self):
        return True

    def exists(self):
        return True

class LoopbackDisk(Disk):
    def __init__(self, lofile, size):
        Disk.__init__(self, size)
        self.lofile = lofile

    def fixed(self):
        return False

    def exists(self):
        return os.path.exists(self.lofile)

    def create(self):
        if self.device is not None:
            return

        losetupProc = subprocess.Popen(["/sbin/losetup", "-f"],
                                       stdout=subprocess.PIPE)
        losetupOutput = losetupProc.communicate()[0]

        if losetupProc.returncode:
            raise MountError("Failed to allocate loop device for '%s'" %
                             self.lofile)

        device = losetupOutput.split()[0]

        logging.debug("Losetup add %s mapping to %s"  % (device, self.lofile))
        rc = subprocess.call(["/sbin/losetup", device, self.lofile])
        if rc != 0:
            raise MountError("Failed to allocate loop device for '%s'" %
                             self.lofile)
        self.device = device

    def cleanup(self):
        if self.device is None:
            return
        logging.debug("Losetup remove %s" % self.device)
        rc = subprocess.call(["/sbin/losetup", "-d", self.device])
        self.device = None



class SparseLoopbackDisk(LoopbackDisk):
    def __init__(self, lofile, size):
        LoopbackDisk.__init__(self, lofile, size)

    def expand(self, create = False, size = None):
        flags = os.O_WRONLY
        if create:
            flags |= os.O_CREAT
            makedirs(os.path.dirname(self.lofile))

        if size is None:
            size = self.size

        logging.debug("Extending sparse file %s to %d" % (self.lofile, size))
        fd = os.open(self.lofile, flags)

        os.lseek(fd, size, 0)
        os.write(fd, '\x00')
        os.close(fd)

    def truncate(self, size = None):
        if size is None:
            size = self.size

        logging.debug("Truncating sparse file %s to %d" % (self.lofile, size))
        fd = os.open(self.lofile, os.O_WRONLY)
        os.ftruncate(fd, size)
        os.close(fd)

    def create(self):
        self.expand(create = True)
        LoopbackDisk.create(self)

class Mount:
    def __init__(self, mountdir):
        self.mountdir = mountdir

    def cleanup(self):
        self.unmount()

    def mount(self):
        pass

    def unmount(self):
        pass

class DiskMount(Mount):
    def __init__(self, disk, mountdir, fstype = None, rmmountdir = True):
        Mount.__init__(self, mountdir)

        self.disk = disk
        self.fstype = fstype
        self.rmmountdir = rmmountdir

        self.mounted = False
        self.rmdir   = False

    def cleanup(self):
        Mount.cleanup(self)
        self.disk.cleanup()

    def unmount(self):
        if self.mounted:
            logging.debug("Unmounting directory %s" % self.mountdir)
            rc = subprocess.call(["/bin/umount", self.mountdir])
            if rc == 0:
                self.mounted = False

        if self.rmdir and not self.mounted:
            try:
                os.rmdir(self.mountdir)
            except OSError, e:
                pass
            self.rmdir = False


    def __create(self):
        self.disk.create()


    def mount(self):
        if self.mounted:
            return

        if not os.path.isdir(self.mountdir):
            logging.debug("Creating mount point %s" % self.mountdir)
            os.makedirs(self.mountdir)
            self.rmdir = self.rmmountdir

        self.__create()

        logging.debug("Mounting %s at %s" % (self.disk.device, self.mountdir))
        args = [ "/bin/mount", self.disk.device, self.mountdir ]
        if self.fstype:
            args.extend(["-t", self.fstype])

        rc = subprocess.call(args)
        if rc != 0:
            raise MountError("Failed to mount '%s' to '%s'" %
                             (self.disk.device, self.mountdir))

        self.mounted = True

class ExtDiskMount(DiskMount):
    def __init__(self, disk, mountdir, fstype, blocksize, fslabel, rmmountdir=True):
        DiskMount.__init__(self, disk, mountdir, fstype, rmmountdir)
        self.blocksize = blocksize
        self.fslabel = fslabel

    def __format_filesystem(self):
        logging.debug("Formating %s filesystem on %s" % (self.fstype, self.disk.device))
        rc = subprocess.call(["/sbin/mkfs." + self.fstype,
                              "-F", "-L", self.fslabel,
                              "-m", "1", "-b", str(self.blocksize),
                              self.disk.device])
        #                      str(self.disk.size / self.blocksize)])
        if rc != 0:
            raise MountError("Error creating %s filesystem" % (self.fstype,))
        logging.debug("Tuning filesystem on %s" % self.disk.device)
        subprocess.call(["/sbin/tune2fs", "-c0", "-i0", "-Odir_index",
                         "-ouser_xattr,acl", self.disk.device])

    def __resize_filesystem(self, size = None):
        current_size = os.stat(self.disk.lofile)[stat.ST_SIZE]

        if size is None:
            size = self.size

        if size == current_size:
            return

        if size > current_size:
            self.expand(size)

        self.__fsck()

        resize2fs(self.disk.lofile, size)
        return size

    def __create(self):
        resize = False
        if not self.disk.fixed() and self.disk.exists():
            resize = True

        self.disk.create()

        if resize:
            self.__resize_filesystem()
        else:
            self.__format_filesystem()

    def mount(self):
        self.__create()
        DiskMount.mount(self)

    def __fsck(self):
        logging.debug("Checking filesystem %s" % self.disk.lofile)
        subprocess.call(["/sbin/e2fsck", "-f", "-y", self.disk.lofile])

    def __get_size_from_filesystem(self):
        def parse_field(output, field):
            for line in output.split("\n"):
                if line.startswith(field + ":"):
                    return line[len(field) + 1:].strip()

            raise KeyError("Failed to find field '%s' in output" % field)

        dev_null = os.open("/dev/null", os.O_WRONLY)
        try:
            out = subprocess.Popen(['/sbin/dumpe2fs', '-h', self.disk.lofile],
                                   stdout = subprocess.PIPE,
                                   stderr = dev_null).communicate()[0]
        finally:
            os.close(dev_null)

        return int(parse_field(out, "Block count")) * self.blocksize

    def __resize_to_minimal(self):
        self.__fsck()

        #
        # Use a binary search to find the minimal size
        # we can resize the image to
        #
        bot = 0
        top = self.__get_size_from_filesystem()
        while top != (bot + 1):
            t = bot + ((top - bot) / 2)

            if not resize2fs(self.disk.lofile, t):
                top = t
            else:
                bot = t
        return top

    def resparse(self, size = None):
        self.cleanup()
        minsize = self.__resize_to_minimal()
        self.disk.truncate(minsize)
        return minsize

class DeviceMapperSnapshot(object):
    def __init__(self, imgloop, cowloop):
        self.imgloop = imgloop
        self.cowloop = cowloop

        self.__created = False
        self.__name = None

    def get_path(self):
        if self.__name is None:
            return None
        return os.path.join("/dev/mapper", self.__name)
    path = property(get_path)

    def create(self):
        if self.__created:
            return

        self.imgloop.create()
        self.cowloop.create()

        self.__name = "imgcreate-%d-%d" % (os.getpid(),
                                           random.randint(0, 2**16))

        size = os.stat(self.imgloop.lofile)[stat.ST_SIZE]

        table = "0 %d snapshot %s %s p 8" % (size / 512,
                                             self.imgloop.device,
                                             self.cowloop.device)

        args = ["/sbin/dmsetup", "create", self.__name, "--table", table]
        if subprocess.call(args) != 0:
            self.cowloop.cleanup()
            self.imgloop.cleanup()
            raise SnapshotError("Could not create snapshot device using: " +
                                string.join(args, " "))

        self.__created = True

    def remove(self, ignore_errors = False):
        if not self.__created:
            return

        rc = subprocess.call(["/sbin/dmsetup", "remove", self.__name])
        if not ignore_errors and rc != 0:
            raise SnapshotError("Could not remove snapshot device")

        self.__name = None
        self.__created = False

        self.cowloop.cleanup()
        self.imgloop.cleanup()

    def get_cow_used(self):
        if not self.__created:
            return 0

        dev_null = os.open("/dev/null", os.O_WRONLY)
        try:
            out = subprocess.Popen(["/sbin/dmsetup", "status", self.__name],
                                   stdout = subprocess.PIPE,
                                   stderr = dev_null).communicate()[0]
        finally:
            os.close(dev_null)

        #
        # dmsetup status on a snapshot returns e.g.
        #   "0 8388608 snapshot 416/1048576"
        # or, more generally:
        #   "A B snapshot C/D"
        # where C is the number of 512 byte sectors in use
        #
        try:
            return int((out.split()[3]).split('/')[0]) * 512
        except ValueError:
            raise SnapshotError("Failed to parse dmsetup status: " + out)

def create_image_minimizer(path, image, minimal_size):
    """
    Builds a copy-on-write image which can be used to
    create a device-mapper snapshot of an image where
    the image's filesystem is as small as possible

    The steps taken are:
      1) Create a sparse COW
      2) Loopback mount the image and the COW
      3) Create a device-mapper snapshot of the image
         using the COW
      4) Resize the filesystem to the minimal size
      5) Determine the amount of space used in the COW
      6) Restroy the device-mapper snapshot
      7) Truncate the COW, removing unused space
      8) Create a squashfs of the COW
    """
    imgloop = LoopbackDisk(image, None) # Passing bogus size - doesn't matter

    cowloop = SparseLoopbackDisk(os.path.join(os.path.dirname(path), "osmin"),
                                 64L * 1024L * 1024L)

    snapshot = DeviceMapperSnapshot(imgloop, cowloop)

    try:
        snapshot.create()

        resize2fs(snapshot.path, minimal_size)

        cow_used = snapshot.get_cow_used()
    finally:
        snapshot.remove(ignore_errors = (not sys.exc_info()[0] is None))

    cowloop.truncate(cow_used)

    mksquashfs(cowloop.lofile, path)

    os.unlink(cowloop.lofile)

