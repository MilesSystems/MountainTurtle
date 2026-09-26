#import <Foundation/Foundation.h>
#import <CoreServices/CoreServices.h>

// Exercise the native catalog rename, including its destination lookup, rather
// than bypassing it with POSIX rename(). Used only on isolated test mounts.
int main(int argc, char **argv) {
    @autoreleasepool {
        if (argc != 3) return 2;
        NSString *name = [NSString stringWithUTF8String:argv[2]];
        if (!name || name.length > 255) return 2;
        FSRef source, destination;
        OSStatus status = FSPathMakeRef((const UInt8 *)argv[1], &source, NULL);
        if (status == noErr) {
            UniChar characters[255];
            [name getCharacters:characters];
            status = FSRenameUnicode(&source, (UniCharCount)name.length, characters,
                                     kTextEncodingUnknown, &destination);
        }
        printf("%d\n", (int)status);
        return status == noErr ? 0 : 1;
    }
}
