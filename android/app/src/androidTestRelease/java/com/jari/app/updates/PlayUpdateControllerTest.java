package com.jari.app.updates;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import android.app.Activity;
import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import com.google.android.play.core.appupdate.testing.FakeAppUpdateManager;
import com.google.android.play.core.install.model.AppUpdateType;
import com.jari.app.MainActivity;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.Test;
import org.junit.runner.RunWith;

@RunWith(AndroidJUnit4.class)
public class PlayUpdateControllerTest {
    @Test
    public void cancellationCompletesAndANewCheckCanStartAFreshImmediateFlow() throws Exception {
        try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
            AtomicReference<FakeAppUpdateManager> manager = new AtomicReference<>();
            AtomicReference<PlayUpdateController> controller = new AtomicReference<>();
            scenario.onActivity(activity -> {
                FakeAppUpdateManager fake = new FakeAppUpdateManager(activity);
                fake.setUpdateAvailable(42300, AppUpdateType.IMMEDIATE);
                manager.set(fake);
                controller.set(new PlayUpdateController(fake, activity, 42201));
            });

            assertEquals(42300, checkAvailable(controller.get()));
            AtomicInteger firstResult = new AtomicInteger(Integer.MIN_VALUE);
            CountDownLatch firstFinished = new CountDownLatch(1);
            scenario.onActivity(activity -> controller.get().apply(new Result(firstResult, firstFinished)));
            awaitFlowVisible(manager.get());
            scenario.onActivity(activity -> manager.get().userRejectsUpdate());
            scenario.onActivity(activity -> controller.get().onActivityResult(Activity.RESULT_CANCELED));
            assertTrue("first update flow did not finish within 5s", firstFinished.await(5, TimeUnit.SECONDS));
            assertEquals(Activity.RESULT_CANCELED, firstResult.get());

            manager.get().setUpdateAvailable(42301, AppUpdateType.IMMEDIATE);
            assertEquals(42301, checkAvailable(controller.get()));
            scenario.onActivity(activity -> controller.get().apply(new Result(new AtomicInteger(), new CountDownLatch(1))));
            awaitFlowVisible(manager.get());
        }
    }

    @Test
    public void resumeRestartsAnImmediateUpdateAlreadyInProgress() throws Exception {
        try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
            AtomicReference<FakeAppUpdateManager> manager = new AtomicReference<>();
            AtomicReference<PlayUpdateController> controller = new AtomicReference<>();
            scenario.onActivity(activity -> {
                FakeAppUpdateManager fake = new FakeAppUpdateManager(activity);
                fake.setUpdateAvailable(42300, AppUpdateType.IMMEDIATE);
                manager.set(fake);
                controller.set(new PlayUpdateController(fake, activity, 42201));
            });
            checkAvailable(controller.get());
            scenario.onActivity(activity -> controller.get().apply(new Result(new AtomicInteger(), new CountDownLatch(1))));
            awaitFlowVisible(manager.get());
            scenario.onActivity(activity -> {
                manager.get().userAcceptsUpdate();
                manager.get().downloadStarts();
                controller.get().onResume();
            });
            awaitFlowVisible(manager.get());
            AtomicReference<Integer> typeInProgress = new AtomicReference<>();
            scenario.onActivity(activity -> typeInProgress.set(manager.get().getTypeForUpdateInProgress()));
            assertEquals(Integer.valueOf(AppUpdateType.IMMEDIATE), typeInProgress.get());
        }
    }

    @Test
    public void availableUpdateWithoutImmediatePermissionIsNotReportedAsCurrent() throws Exception {
        try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
            AtomicReference<PlayUpdateController> controller = new AtomicReference<>();
            scenario.onActivity(activity -> {
                FakeAppUpdateManager fake = new FakeAppUpdateManager(activity);
                fake.setUpdateAvailable(42300, AppUpdateType.FLEXIBLE);
                controller.set(new PlayUpdateController(fake, activity, 42201));
            });
            AtomicInteger current = new AtomicInteger();
            AtomicInteger errors = new AtomicInteger();
            CountDownLatch finished = new CountDownLatch(1);
            controller.get().check(new CountingResult(current, errors, finished));

            assertTrue("update check did not finish within 5s", finished.await(5, TimeUnit.SECONDS));
            assertEquals(0, current.get());
            assertEquals(1, errors.get());
        }
    }

    @Test
    public void destroyDropsAPendingActivityResultCallback() throws Exception {
        try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
            AtomicReference<FakeAppUpdateManager> manager = new AtomicReference<>();
            AtomicReference<PlayUpdateController> controller = new AtomicReference<>();
            scenario.onActivity(activity -> {
                FakeAppUpdateManager fake = new FakeAppUpdateManager(activity);
                fake.setUpdateAvailable(42300, AppUpdateType.IMMEDIATE);
                manager.set(fake);
                controller.set(new PlayUpdateController(fake, activity, 42201));
            });
            checkAvailable(controller.get());
            AtomicInteger results = new AtomicInteger();
            CountDownLatch finished = new CountDownLatch(1);
            scenario.onActivity(activity -> controller.get().apply(new CountingResult(
                new AtomicInteger(), results, finished
            )));
            awaitFlowVisible(manager.get());

            scenario.onActivity(activity -> {
                controller.get().destroy();
                controller.get().onActivityResult(Activity.RESULT_CANCELED);
            });

            assertEquals(0, results.get());
            assertFalse("destroyed controller must not deliver the pending result", finished.await(200, TimeUnit.MILLISECONDS));
        }
    }

    private int checkAvailable(PlayUpdateController controller) throws Exception {
        AtomicInteger versionCode = new AtomicInteger();
        CountDownLatch finished = new CountDownLatch(1);
        controller.check(new PlayUpdateController.Callback() {
            @Override public void onCurrent() { finished.countDown(); }
            @Override public void onAvailable(int value) { versionCode.set(value); finished.countDown(); }
            @Override public void onResult(int ignored) { finished.countDown(); }
            @Override public void onError(Exception ignored) { finished.countDown(); }
        });
        assertTrue("update check did not finish within 5s", finished.await(5, TimeUnit.SECONDS));
        return versionCode.get();
    }

    /**
     * PlayUpdateController.apply()/onResume() start the flow from an async getAppUpdateInfo()
     * success listener, so the flow only becomes visible after the main looper drains.
     */
    private static void awaitFlowVisible(FakeAppUpdateManager manager) {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        AtomicBoolean visible = new AtomicBoolean();
        do {
            InstrumentationRegistry.getInstrumentation().waitForIdleSync();
            InstrumentationRegistry.getInstrumentation().runOnMainSync(
                () -> visible.set(manager.isImmediateFlowVisible())
            );
            if (visible.get()) return;
            try {
                Thread.sleep(20);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
                throw new AssertionError("Interrupted while waiting for the immediate update flow", interrupted);
            }
        } while (System.nanoTime() < deadline);
        throw new AssertionError("Immediate update flow did not become visible within 5s");
    }

    private static final class Result implements PlayUpdateController.Callback {
        private final AtomicInteger result;
        private final CountDownLatch finished;

        Result(AtomicInteger result, CountDownLatch finished) {
            this.result = result;
            this.finished = finished;
        }

        @Override public void onCurrent() {}
        @Override public void onAvailable(int ignored) {}
        @Override public void onResult(int value) { result.set(value); finished.countDown(); }
        @Override public void onError(Exception ignored) { finished.countDown(); }
    }

    private static final class CountingResult implements PlayUpdateController.Callback {
        private final AtomicInteger current;
        private final AtomicInteger errorsOrResults;
        private final CountDownLatch finished;

        CountingResult(AtomicInteger current, AtomicInteger errorsOrResults, CountDownLatch finished) {
            this.current = current;
            this.errorsOrResults = errorsOrResults;
            this.finished = finished;
        }

        @Override public void onCurrent() { current.incrementAndGet(); finished.countDown(); }
        @Override public void onAvailable(int ignored) { finished.countDown(); }
        @Override public void onResult(int ignored) { errorsOrResults.incrementAndGet(); finished.countDown(); }
        @Override public void onError(Exception ignored) { errorsOrResults.incrementAndGet(); finished.countDown(); }
    }
}
