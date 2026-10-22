// MiBand AirMouse launcher: the app's Dock presence.
//
// Runs ~/airmouse/app.py as a child process (spawned, not exec'd, so this bundle stays the
// "responsible" app: macOS reads the Bluetooth usage text from our Info.plist and files the
// Bluetooth/Accessibility permissions under "MiBand AirMouse", not Python).
// Clicking the Dock icon or switching to the app sends SIGUSR1 (show the control window);
// quitting sends SIGTERM so the Python side can close the band's hidden workout first.
#import <Cocoa/Cocoa.h>
#include <signal.h>
#include <spawn.h>
#include <sys/wait.h>

extern char **environ;
static pid_t child = 0;

static BOOL reapChild(void) {
    int status;
    if (child > 0 && waitpid(child, &status, WNOHANG) == child) {
        child = 0;
    }
    return child == 0;
}

@interface Launcher : NSObject <NSApplicationDelegate>
@end

@implementation Launcher

- (void)applicationDidFinishLaunching:(NSNotification *)note {
    NSString *home = NSHomeDirectory();
    NSString *python = [home stringByAppendingPathComponent:@"airmouse/venv/bin/python"];
    NSString *script = [home stringByAppendingPathComponent:@"airmouse/app.py"];
    char *argv[] = {(char *)python.UTF8String, (char *)script.UTF8String, NULL};
    // Start the child with SIGUSR1 blocked: a "show window" arriving before Python has
    // installed its handler would otherwise kill it. Python unblocks it once ready.
    posix_spawnattr_t attr;
    posix_spawnattr_init(&attr);
    sigset_t mask;
    sigemptyset(&mask);
    sigaddset(&mask, SIGUSR1);
    posix_spawnattr_setsigmask(&attr, &mask);
    posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETSIGMASK);
    int err = posix_spawn(&child, python.UTF8String, NULL, &attr, argv, environ);
    posix_spawnattr_destroy(&attr);
    if (err != 0) {
        NSAlert *alert = [NSAlert new];
        alert.messageText = @"Không khởi động được MiBand AirMouse";
        alert.informativeText = [NSString stringWithFormat:@"Không chạy được %@", python];
        [alert runModal];
        [NSApp terminate:nil];
        return;
    }
    // When the Python side quits on its own (its "Thoát" button), quit with it.
    [NSTimer scheduledTimerWithTimeInterval:0.5 repeats:YES block:^(NSTimer *timer) {
        if (reapChild()) {
            [timer invalidate];
            [NSApp terminate:nil];
        }
    }];
}

- (void)showWindow {
    if (child > 0) {
        kill(child, SIGUSR1);
    }
}

- (BOOL)applicationShouldHandleReopen:(NSApplication *)app hasVisibleWindows:(BOOL)visible {
    [self showWindow];  // Dock icon clicked
    return NO;
}

- (void)applicationDidBecomeActive:(NSNotification *)note {
    [self showWindow];  // Cmd+Tab / Launchpad / Finder brought us forward
}

- (NSApplicationTerminateReply)applicationShouldTerminate:(NSApplication *)app {
    if (child > 0) {
        kill(child, SIGTERM);
        for (int i = 0; i < 50 && !reapChild(); i++) {  // up to 5s to close the band's workout
            usleep(100000);
        }
        if (child > 0) {
            kill(child, SIGKILL);
        }
    }
    return NSTerminateNow;
}

@end

int main(void) {
    @autoreleasepool {
        NSApplication *app = [NSApplication sharedApplication];
        Launcher *launcher = [Launcher new];
        app.delegate = launcher;

        // App menu, so Cmd+Q and "Show window" work while the Dock app is frontmost.
        NSMenu *bar = [NSMenu new];
        NSMenuItem *appItem = [NSMenuItem new];
        [bar addItem:appItem];
        NSMenu *appMenu = [[NSMenu alloc] initWithTitle:@"MiBand AirMouse"];
        NSMenuItem *show = [appMenu addItemWithTitle:@"Mở cửa sổ điều khiển"
                                              action:@selector(showWindow)
                                       keyEquivalent:@"o"];
        show.target = launcher;
        [appMenu addItem:[NSMenuItem separatorItem]];
        [appMenu addItemWithTitle:@"Thoát MiBand AirMouse" action:@selector(terminate:) keyEquivalent:@"q"];
        appItem.submenu = appMenu;
        app.mainMenu = bar;
        [app setActivationPolicy:NSApplicationActivationPolicyRegular];
        [app run];
    }
    return 0;
}
