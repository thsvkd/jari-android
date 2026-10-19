package com.jari.app.updates;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import android.app.Activity;
import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import com.google.android.play.core.appupdate.AppUpdateManager;
import com.google.android.play.core.appupdate.testing.FakeAppUpdateManager;
import com.google.android.play.core.install.model.AppUpdateType;
import com.jari.app.MainActivity;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Supplier;
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
            assertTrue("first apply should show the immediate flow",
                onMain(scenario, () -> manager.get().isImmediateFlowVisible()));
            scenario.onActivity(activity -> manager.get().userRejectsUpdate());
            controller.get().onActivityResult(Activity.RESULT_CANCELED);
            assertTrue("cancelled flow should report its result", firstFinished.await(5, TimeUnit.SECONDS));
            assertEquals(Activity.RESULT_CANCELED, firstResult.get());

            manager.get().setUpdateAvailable(42301, AppUpdateType.IMMEDIATE);
            assertEquals(42301, checkAvailable(controller.get()));
            scenario.onActivity(activity -> controller.get().apply(new Result(new AtomicInteger(), new CountDownLatch(1))));
            assertTrue("apply after a new check should show a fresh immediate flow",
                onMain(scenario, () -> manager.get().isImmediateFlowVisible()));
        }
    }

    @Test
    public void resumeRestartsAnImmediateUpdateAlreadyInProgress() throws Exception {
        try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
            AtomicReference<FakeAppUpdateManager> manager = new AtomicReference<>();
            AtomicReference<PlayUpdateController> controller = new AtomicReference<>();
            AtomicInteger flowRequests = new AtomicInteger();
            scenario.onActivity(activity -> {
                FakeAppUpdateManager fake = new FakeAppUpdateManager(activity);
                fake.setUpdateAvailable(42300, AppUpdateType.IMMEDIATE);
                manager.set(fake);
                controller.set(new PlayUpdateController(countingFlowRequests(fake, flowRequests), activity, 42201));
            });
            checkAvailable(controller.get());
            scenario.onActivity(activity -> controller.get().apply(new Result(new AtomicInteger(), new CountDownLatch(1))));
            assertEquals("apply should request the immediate flow once", 1, (int) onMain(scenario, flowRequests::get));
            scenario.onActivity(activity -> {
                manager.get().userAcceptsUpdate();
                manager.get().downloadStarts();
                controller.get().onResume();
            });
            // The fake keeps the first flow visible after acceptance and gives in-progress info no flow
            // intents (startUpdateFlowForResult returns false), so only the request proves the restart.
            assertEquals("onResume should request the in-progress immediate flow again",
                2, (int) onMain(scenario, flowRequests::get));
            assertEquals(Integer.valueOf(AppUpdateType.IMMEDIATE),
                onMain(scenario, () -> manager.get().getTypeForUpdateInProgress()));
            assertTrue(onMain(scenario, () -> manager.get().isImmediateFlowVisible()));
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

            assertTrue("check should finish", finished.await(5, TimeUnit.SECONDS));
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
            assertTrue("apply should show the immediate flow",
                onMain(scenario, () -> manager.get().isImmediateFlowVisible()));

            controller.get().destroy();
            controller.get().onActivityResult(Activity.RESULT_CANCELED);

            assertEquals(0, results.get());
            assertFalse(finished.await(200, TimeUnit.MILLISECONDS));
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
        assertTrue("check should finish", finished.await(5, TimeUnit.SECONDS));
        return versionCode.get();
    }

    // Play Tasks post listeners to the main looper even when already complete, so apply() and
    // onResume() reach the fake after onActivity() returns. onActivity() drains the main looper
    // before running, and reading there also sees the fake's plain fields from the main thread.
    private static <T> T onMain(ActivityScenario<MainActivity> scenario, Supplier<T> read) {
        AtomicReference<T> value = new AtomicReference<>();
        scenario.onActivity(activity -> value.set(read.get()));
        return value.get();
    }

    private static AppUpdateManager countingFlowRequests(FakeAppUpdateManager fake, AtomicInteger requests) {
        return (AppUpdateManager) Proxy.newProxyInstance(
            AppUpdateManager.class.getClassLoader(),
            new Class<?>[] {AppUpdateManager.class},
            (proxy, method, args) -> {
                if (method.getName().equals("startUpdateFlowForResult")) requests.incrementAndGet();
                try {
                    return method.invoke(fake, args);
                } catch (InvocationTargetException error) {
                    throw error.getCause();
                }
            }
        );
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
