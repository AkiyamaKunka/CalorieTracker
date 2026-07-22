# R8 keep rules for reflective code paths in release builds.
#
# WorkManager (via the workmanager plugin's background photo scan) creates
# its Room database and worker classes by REFLECTION. R8 stripped the
# WorkDatabase_Impl no-arg constructor, crashing every release launch at
# androidx.startup.InitializationProvider before any app code ran:
#   NoSuchMethodException: androidx.work.impl.WorkDatabase_Impl.<init> []
# (Debug builds don't minify, which is why all E2E runs passed.)
-keep class androidx.work.impl.WorkDatabase_Impl { *; }
-keep class androidx.work.impl.** { *; }
-keep class * extends androidx.work.ListenableWorker { <init>(...); }

# The workmanager Flutter plugin resolves its dispatcher reflectively.
-keep class dev.fluttercommunity.workmanager.** { *; }
