#import <Foundation/Foundation.h>
#import <CoreServices/CoreServices.h>
#import <sys/mount.h>
#import <sys/file.h>
#import <sys/stat.h>
#import <fcntl.h>
#import <pwd.h>
#import <unistd.h>

static CFStringRef const ConnectionProperty = CFSTR("io.mountainturtle.connection-id");
static CFStringRef const MountPathProperty = CFSTR("io.mountainturtle.mount-path");

// Finder's supported "Add to Sidebar" command stores actual mounted volumes in
// this list. Apple still exports these public APIs, but deprecated them in 10.11.
// Keep this compatibility layer in a bounded helper: sidebar failure must never
// prevent a drive from mounting or delay the supervisor's other connections.

static int Reply(BOOL success, NSDictionary *fields) {
    NSMutableDictionary *result = [fields mutableCopy];
    result[@"ok"] = @(success);
    NSData *data = [NSJSONSerialization dataWithJSONObject:result options:0 error:nil];
    if (data) fwrite(data.bytes, 1, data.length, stdout);
    fputc('\n', stdout);
    return success ? 0 : 1;
}

static BOOL IsManagedPath(NSString *path, NSString *root) {
    if (![path hasPrefix:@"/"] || ![path isEqualToString:path.stringByStandardizingPath]) return NO;
    if (![path.stringByDeletingLastPathComponent isEqualToString:root]) return NO;
    NSString *name = path.lastPathComponent;
    if (!name.length || [name hasPrefix:@"."] || [name lengthOfBytesUsingEncoding:NSUTF8StringEncoding] > 180) return NO;
    NSCharacterSet *invalid = [NSCharacterSet characterSetWithCharactersInString:@":/\\"];
    return [name rangeOfCharacterFromSet:invalid].location == NSNotFound &&
        [name rangeOfCharacterFromSet:NSCharacterSet.controlCharacterSet].location == NSNotFound;
}

static BOOL IsMountedNFS(NSString *path) {
    struct statfs *mounts = NULL;
    int count = getmntinfo(&mounts, MNT_NOWAIT);
    for (int i = 0; i < count; ++i) {
        if (strcmp(mounts[i].f_fstypename, "nfs") == 0 &&
            strcmp(mounts[i].f_mntonname, path.fileSystemRepresentation) == 0) return YES;
    }
    return NO;
}

static NSMutableDictionary *ReadRegistry(NSString *path) {
    int fd = open(path.fileSystemRepresentation, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (fd < 0) return [NSMutableDictionary dictionary];
    struct stat info;
    if (fstat(fd, &info) != 0 || !S_ISREG(info.st_mode) || info.st_uid != getuid() ||
        (info.st_mode & 077) != 0 || info.st_size < 1 || info.st_size > 65536) {
        close(fd);
        return [NSMutableDictionary dictionary];
    }
    NSMutableData *data = [NSMutableData dataWithLength:65537];
    ssize_t count = read(fd, data.mutableBytes, data.length);
    close(fd);
    if (count < 1 || count > 65536) return [NSMutableDictionary dictionary];
    data.length = (NSUInteger)count;
    id value = [NSJSONSerialization JSONObjectWithData:data options:NSJSONReadingMutableContainers error:nil];
    return [value isKindOfClass:NSMutableDictionary.class] ? value : [NSMutableDictionary dictionary];
}

static BOOL WriteRegistry(NSDictionary *value, NSString *path) {
    NSData *data = [NSJSONSerialization dataWithJSONObject:value options:0 error:nil];
    if (!data || data.length > 65536) return NO;
    NSString *temporary = [path stringByAppendingFormat:@".%@.tmp", NSUUID.UUID.UUIDString];
    int fd = open(temporary.fileSystemRepresentation, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0) return NO;
    const uint8_t *cursor = data.bytes;
    NSUInteger remaining = data.length;
    BOOL success = YES;
    while (remaining > 0) {
        ssize_t count = write(fd, cursor, remaining);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) { success = NO; break; }
        cursor += count;
        remaining -= (NSUInteger)count;
    }
    close(fd);
    if (success) success = rename(temporary.fileSystemRepresentation, path.fileSystemRepresentation) == 0;
    if (!success) unlink(temporary.fileSystemRepresentation);
    return success;
}

int main(int argc, const char **argv) {
    @autoreleasepool {
        if (argc != 4 || strcmp(argv[1], "ensure") != 0) {
            return Reply(NO, @{@"error": @"Expected ensure, a connection ID, and its mounted drive path"});
        }
        NSString *identity = [NSString stringWithUTF8String:argv[2]];
        NSString *path = [NSString stringWithUTF8String:argv[3]];
        NSUUID *uuid = identity ? [[NSUUID alloc] initWithUUIDString:identity] : nil;
        struct passwd *user = getpwuid(getuid());
        NSString *home = user && user->pw_dir ? [NSString stringWithUTF8String:user->pw_dir] : nil;
        NSString *root = [home stringByAppendingPathComponent:@"Mountain Turtle"];
        if (!uuid || !home || !path || !IsManagedPath(path, root)) {
            return Reply(NO, @{@"error": @"This is not a saved Mountain Turtle drive path"});
        }
        if (!IsMountedNFS(path)) {
            return Reply(NO, @{@"error": @"The drive is not mounted; its sidebar entry was not changed"});
        }
        identity = uuid.UUIDString.lowercaseString;

        NSString *support = [home stringByAppendingPathComponent:@"Library/Application Support/Mountain Turtle"];
        NSError *directoryError = nil;
        if (![NSFileManager.defaultManager createDirectoryAtPath:support withIntermediateDirectories:YES
                    attributes:@{NSFilePosixPermissions: @0700} error:&directoryError]) {
            return Reply(NO, @{@"error": @"Could not open Mountain Turtle's sidebar settings"});
        }
        NSString *lockPath = [support stringByAppendingPathComponent:@"sidebar-items.lock"];
        int lock = open(lockPath.fileSystemRepresentation, O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK, 0600);
        struct stat lockInfo;
        if (lock < 0 || fstat(lock, &lockInfo) != 0 || !S_ISREG(lockInfo.st_mode) ||
            lockInfo.st_uid != getuid() || (lockInfo.st_mode & 077) != 0 || flock(lock, LOCK_EX | LOCK_NB) != 0) {
            if (lock >= 0) close(lock);
            return Reply(NO, @{@"error": @"Another sidebar update is already running"});
        }
        NSString *registryPath = [support stringByAppendingPathComponent:@"sidebar-items.json"];
        NSMutableDictionary *registry = ReadRegistry(registryPath);
        id previousValue = registry[identity];
        NSDictionary *previous = [previousValue isKindOfClass:NSDictionary.class] ? previousValue : @{};
        NSString *previousPath = [previous[@"path"] isKindOfClass:NSString.class] ? previous[@"path"] : nil;
        if (previousPath && !IsManagedPath(previousPath, root)) previousPath = nil;
        NSNumber *previousID = [previous[@"itemID"] isKindOfClass:NSNumber.class] ? previous[@"itemID"] : nil;

        LSSharedFileListRef list = LSSharedFileListCreate(NULL, kLSSharedFileListFavoriteVolumes, NULL);
        if (!list) {
            close(lock);
            return Reply(NO, @{@"error": @"Finder's mounted-volume sidebar list is unavailable"});
        }
        UInt32 seed = 0;
        CFArrayRef snapshot = LSSharedFileListCopySnapshot(list, &seed);
        if (!snapshot) {
            CFRelease(list);
            close(lock);
            return Reply(NO, @{@"error": @"Could not read Finder's mounted-volume sidebar list"});
        }
        NSMutableArray *matches = [NSMutableArray array];
        LSSharedFileListItemRef position = kLSSharedFileListItemLast;
        LSSharedFileListItemRef preceding = kLSSharedFileListItemBeforeFirst;
        for (id candidate in (__bridge NSArray *)snapshot) {
            LSSharedFileListItemRef item = (__bridge LSSharedFileListItemRef)candidate;
            UInt32 itemID = LSSharedFileListItemGetID(item);
            NSString *name = CFBridgingRelease(LSSharedFileListItemCopyDisplayName(item));
            BOOL recordedName = [name isEqualToString:previousPath.lastPathComponent] ||
                [name isEqualToString:path.lastPathComponent];
            BOOL recordedOwnership = previousPath && previousID &&
                itemID == previousID.unsignedIntValue && recordedName;
            id markedIdentity = CFBridgingRelease(LSSharedFileListItemCopyProperty(item, ConnectionProperty));
            id markedPath = CFBridgingRelease(LSSharedFileListItemCopyProperty(item, MountPathProperty));
            BOOL markedOwnership = [markedIdentity isKindOfClass:NSString.class] &&
                [markedIdentity isEqualToString:identity] && [markedPath isKindOfClass:NSString.class] &&
                IsManagedPath(markedPath, root);
            // Never resolve a volume bookmark here. Even the no-mount/no-UI
            // flags can block on an old NFS server after reconnect. Our private
            // record or persistent item marker proves ownership without I/O to
            // the volume. Unowned, same-name user favorites are left untouched.
            if (recordedOwnership || markedOwnership) {
                if (matches.count == 0) position = preceding;
                [matches addObject:candidate];
            }
            preceding = item;
        }

        // The old bookmark may identify the previous NFS mount. Insert a bookmark
        // for this mount first, then remove only entries we own for this saved
        // connection. This restores native ejectable
        // Locations entries without changing the order of unrelated volumes.
        if (!IsMountedNFS(path)) {
            CFRelease(snapshot);
            CFRelease(list);
            close(lock);
            return Reply(NO, @{@"error": @"The drive was ejected before its sidebar entry could be updated"});
        }
        NSURL *url = [NSURL fileURLWithPath:path isDirectory:YES];
        NSDictionary *properties = @{(__bridge NSString *)ConnectionProperty: identity,
                                     (__bridge NSString *)MountPathProperty: path};
        LSSharedFileListItemRef fresh = LSSharedFileListInsertItemURL(list, position,
            (__bridge CFStringRef)path.lastPathComponent, NULL, (__bridge CFURLRef)url,
            (__bridge CFDictionaryRef)properties, NULL);
        if (!fresh) {
            CFRelease(snapshot);
            CFRelease(list);
            close(lock);
            return Reply(NO, @{@"error": @"Finder did not accept this mounted drive in its sidebar"});
        }
        UInt32 freshID = LSSharedFileListItemGetID(fresh);
        NSMutableArray *replaced = [NSMutableArray array];
        NSMutableArray *warnings = [NSMutableArray array];
        for (id candidate in matches) {
            LSSharedFileListItemRef item = (__bridge LSSharedFileListItemRef)candidate;
            UInt32 itemID = LSSharedFileListItemGetID(item);
            if (itemID == freshID) continue;
            if (LSSharedFileListItemRemove(list, item) == noErr) [replaced addObject:@(itemID)];
            else [warnings addObject:@"A previous entry for this drive could not be removed"];
        }
        registry[identity] = @{@"itemID": @(freshID), @"path": path};
        if (!WriteRegistry(registry, registryPath)) {
            [warnings addObject:@"The sidebar entry was added, but its local settings could not be saved"];
        }
        CFRelease(fresh);
        CFRelease(snapshot);
        CFRelease(list);
        close(lock);
        NSMutableDictionary *result = [@{@"itemID": @(freshID), @"replacedIDs": replaced} mutableCopy];
        if (warnings.count) result[@"warnings"] = warnings;
        return Reply(YES, result);
    }
}
