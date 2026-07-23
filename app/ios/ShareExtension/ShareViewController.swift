import receive_sharing_intent

// The plugin's RSIShareViewController writes the shared images into the
// App Group container and bounces to the host app via the
// ShareMedia-<bundle-id> URL scheme declared in Runner/Info.plist.
// All behavior lives in the superclass; this subclass exists so the
// storyboard can name a class inside THIS target.
class ShareViewController: RSIShareViewController {}
