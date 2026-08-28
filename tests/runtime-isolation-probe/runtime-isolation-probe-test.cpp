// Regression gate for issue #243 / ADR-PULSAR-DUAL-LANE-001.
//
// The test deliberately runs the production ExclusiveLease implementation.
// It proves four independent runtime namespaces can be held concurrently,
// that the historical DirectShow alias has exactly one holder, that a second
// claimant is refused deterministically, and that release/reacquisition and
// same-runtime collision recovery do not rely on stale marker text.

#include "runtime-identity.h"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <memory>
#include <random>
#include <string>
#include <thread>
#include <vector>

#define PULSAR_CHECK(expr)                                                                  \
    do {                                                                                    \
        if (!(expr)) {                                                                      \
            std::fprintf(stderr, "CHECK FAILED: %s (%s:%d)\n", #expr, __FILE__, __LINE__); \
            std::exit(EXIT_FAILURE);                                                        \
        }                                                                                    \
    } while (0)

namespace fs = std::filesystem;
using pulsar_runtime::ExclusiveLease;

namespace {

fs::path scratch_root()
{
    std::mt19937_64 rng(std::random_device{}());
    const fs::path root = fs::temp_directory_path() /
                          ("pulsar-runtime-isolation-" + std::to_string(rng()));
    std::error_code ec;
    fs::remove_all(root, ec);
    fs::create_directories(root, ec);
    PULSAR_CHECK(!ec);
    return root;
}

void test_four_namespaces_and_same_runtime_collision(const fs::path &root)
{
    std::vector<std::unique_ptr<ExclusiveLease>> locks;
    std::vector<std::unique_ptr<ExclusiveLease>> directory_locks;
    std::vector<fs::path> resource_paths;

    for (int i = 0; i < 4; ++i) {
        const std::string id = "probe-runtime-" + std::to_string(i);
        const fs::path dir = root / id;
        const fs::path lock_path = dir / "instance.lock";
        std::error_code ec;
        fs::create_directories(dir, ec);
        PULSAR_CHECK(!ec);

        auto lock = std::make_unique<ExclusiveLease>();
        PULSAR_CHECK(lock->acquire(lock_path, id, "runtime-instance"));
        PULSAR_CHECK(lock->held());
        locks.push_back(std::move(lock));

        auto directory_lock = std::make_unique<ExclusiveLease>();
        PULSAR_CHECK(directory_lock->acquire(dir / ".runtime.lock", id,
                                             "runtime-directory"));
        PULSAR_CHECK(directory_lock->held());
        directory_locks.push_back(std::move(directory_lock));
        resource_paths.push_back(dir / "obs-websocket" / "config.json");
        resource_paths.push_back(dir / "logs" / "pulsar.log");
        resource_paths.push_back(dir / "recordings");
    }

    // Every named path is rooted under a different runtime directory.  This
    // is the inventory that the real bundle creates for four instances.
    for (std::size_t i = 0; i < resource_paths.size(); ++i)
        for (std::size_t j = i + 1; j < resource_paths.size(); ++j)
            PULSAR_CHECK(resource_paths[i] != resource_paths[j]);

    // A second process using the same instance directory cannot silently
    // share its config/log/recording namespace.
    ExclusiveLease collision;
    PULSAR_CHECK(!collision.acquire(root / "probe-runtime-0" / "instance.lock",
                                    "probe-runtime-0-second", "runtime-instance"));
    PULSAR_CHECK(collision.result() == pulsar_runtime::LeaseResult::Refused);
    PULSAR_CHECK(collision.reason().find("already_held") == 0);
    PULSAR_CHECK(collision.holder_runtime_id() == "probe-runtime-0");

    std::fprintf(stdout,
                 "runtime-inventory: instances=4 unique_resources=%zu same_instance=refused\n",
                 resource_paths.size());

    for (auto &lock : locks)
        lock->release();
    for (auto &lock : directory_locks)
        lock->release();

    // Recovery after a clean release is explicit and deterministic.
    ExclusiveLease recovered;
    PULSAR_CHECK(recovered.acquire(root / "probe-runtime-0" / "instance.lock",
                                   "probe-runtime-0-recovered", "runtime-instance"));
    PULSAR_CHECK(recovered.renew());
    recovered.release();
    PULSAR_CHECK(!recovered.held());
    std::fprintf(stdout, "runtime-recovery: released=1 reacquired=1 renewed=1\n");
}

void test_shared_explicit_directory_collision(const fs::path &root)
{
    const fs::path shared_dir = root / "shared-explicit-runtime";
    std::error_code ec;
    fs::create_directories(shared_dir, ec);
    PULSAR_CHECK(!ec);

    ExclusiveLease first;
    PULSAR_CHECK(first.acquire(shared_dir / ".runtime.lock", "explicit-runtime-a",
                               "runtime-directory"));

    ExclusiveLease second;
    PULSAR_CHECK(!second.acquire(shared_dir / ".runtime.lock", "explicit-runtime-b",
                                 "runtime-directory"));
    PULSAR_CHECK(second.result() == pulsar_runtime::LeaseResult::Refused);
    PULSAR_CHECK(second.reason().find("already_held") == 0);
    PULSAR_CHECK(second.holder_runtime_id() == "explicit-runtime-a");
    std::fprintf(stdout,
                 "runtime-directory-collision: holder=explicit-runtime-a claimant=explicit-runtime-b refusal=%s\n",
                 second.reason().c_str());

    first.release();
    PULSAR_CHECK(second.acquire(shared_dir / ".runtime.lock", "explicit-runtime-b",
                                "runtime-directory"));
    second.release();
    std::fprintf(stdout, "runtime-directory-recovery: release=1 reacquire=1\n");
}

void test_alias_singleton_and_concurrent_claimants(const fs::path &root)
{
    const fs::path alias_path = root / "leases" / "directshow-program-preview.lock";
    std::error_code ec;
    fs::create_directories(alias_path.parent_path(), ec);
    PULSAR_CHECK(!ec);

    ExclusiveLease first;
    PULSAR_CHECK(first.acquire(alias_path, "probe-runtime-0", "directshow-legacy-alias"));
    PULSAR_CHECK(first.renew());

    ExclusiveLease second;
    PULSAR_CHECK(!second.acquire(alias_path, "probe-runtime-1", "directshow-legacy-alias"));
    PULSAR_CHECK(second.result() == pulsar_runtime::LeaseResult::Refused);
    PULSAR_CHECK(second.reason().find("already_held") == 0);
    PULSAR_CHECK(second.holder_runtime_id() == "probe-runtime-0");
    std::fprintf(stdout, "legacy-alias: holder=probe-runtime-0 claimant=probe-runtime-1 refusal=%s\n",
                 second.reason().c_str());

    first.release();
    PULSAR_CHECK(second.acquire(alias_path, "probe-runtime-1", "directshow-legacy-alias"));
    second.release();
    std::fprintf(stdout, "legacy-alias-recovery: release=1 reacquire=1\n");

    // Four independent runtime locks race for one alias lock.  Exactly one
    // obtains the compatibility namespace, and every non-holder remains
    // usable through its already-isolated instance namespace.
    std::atomic<int> ready{0};
    std::atomic<bool> start{false};
    std::atomic<int> alias_holders{0};
    std::atomic<int> alias_refusals{0};
    std::vector<std::thread> workers;
    for (int i = 0; i < 4; ++i) {
        workers.emplace_back([&, i] {
            const std::string id = "concurrent-runtime-" + std::to_string(i);
            const fs::path dir = root / id;
            std::error_code worker_ec;
            fs::create_directories(dir, worker_ec);
            PULSAR_CHECK(!worker_ec);
            ExclusiveLease runtime;
            PULSAR_CHECK(runtime.acquire(dir / "instance.lock", id, "runtime-instance"));

            ready.fetch_add(1, std::memory_order_release);
            while (!start.load(std::memory_order_acquire))
                std::this_thread::yield();

            ExclusiveLease alias;
            if (alias.acquire(alias_path, id, "directshow-legacy-alias")) {
                alias_holders.fetch_add(1, std::memory_order_relaxed);
                std::this_thread::sleep_for(std::chrono::milliseconds(30));
                alias.release();
            } else {
                PULSAR_CHECK(alias.result() == pulsar_runtime::LeaseResult::Refused);
                alias_refusals.fetch_add(1, std::memory_order_relaxed);
            }
            runtime.release();
        });
    }
    while (ready.load(std::memory_order_acquire) != 4)
        std::this_thread::yield();
    start.store(true, std::memory_order_release);
    for (auto &worker : workers)
        worker.join();

    PULSAR_CHECK(alias_holders.load() == 1);
    PULSAR_CHECK(alias_refusals.load() == 3);
    std::fprintf(stdout, "legacy-alias-concurrency: claimants=4 holders=%d refusals=%d\n",
                 alias_holders.load(), alias_refusals.load());
}

void test_identity_validation_and_port()
{
    PULSAR_CHECK(pulsar_runtime::is_valid_instance_id("A-0._ok"));
    PULSAR_CHECK(!pulsar_runtime::is_valid_instance_id(""));
    PULSAR_CHECK(!pulsar_runtime::is_valid_instance_id("../escape"));
    PULSAR_CHECK(!pulsar_runtime::is_valid_instance_id("bad/id"));
    PULSAR_CHECK(!pulsar_runtime::is_valid_instance_id(".hidden"));

    ExclusiveLease invalid;
    PULSAR_CHECK(!invalid.acquire(fs::temp_directory_path() / "pulsar-invalid.lock",
                                  "../invalid", "runtime-instance"));
    PULSAR_CHECK(invalid.result() == pulsar_runtime::LeaseResult::Error);
    PULSAR_CHECK(invalid.reason() == "invalid_owner");

    const std::uint16_t port = pulsar_runtime::pick_free_loopback_port();
    PULSAR_CHECK(port != 0);
    std::fprintf(stdout, "identity-validation: valid=1 invalid=5 free_loopback_port=%u\n",
                 static_cast<unsigned>(port));
}

void test_identity_resolution(const fs::path &root)
{
    const fs::path requested_runtime_dir = root / "custom-runtime";
    const fs::path requested_lease_root = root / "custom-leases";
    PULSAR_CHECK(pulsar_runtime::set_process_environment("PULSAR_RUNTIME_INSTANCE_ID",
                                                        "resolved-runtime"));
    PULSAR_CHECK(pulsar_runtime::set_process_environment("PULSAR_RUNTIME_ROOT",
                                                        root.string()));
    PULSAR_CHECK(pulsar_runtime::set_process_environment("PULSAR_RUNTIME_DIR",
                                                        requested_runtime_dir.string()));
    PULSAR_CHECK(pulsar_runtime::set_process_environment("PULSAR_LEGACY_ALIAS_LEASE_ROOT",
                                                        requested_lease_root.string()));

    pulsar_runtime::RuntimeIdentity identity;
    std::string error;
    PULSAR_CHECK(pulsar_runtime::resolve_identity(identity, error));
    PULSAR_CHECK(identity.instance_id == "resolved-runtime");
    PULSAR_CHECK(identity.runtime_dir == requested_runtime_dir);
    PULSAR_CHECK(identity.instance_lease_path ==
                 root / "instances" / "resolved-runtime" / "instance.lock");
    PULSAR_CHECK(identity.runtime_dir_lease_path == requested_runtime_dir / ".runtime.lock");
    PULSAR_CHECK(identity.legacy_alias_lease_path ==
                 requested_lease_root / "directshow-program-preview.lock");
    std::fprintf(stdout, "identity-resolution: explicit_dir=1 shared_instance_lease=1 alias_root=1\n");
}

} // namespace

int main()
{
    const fs::path root = scratch_root();
    test_identity_validation_and_port();
    test_identity_resolution(root);
    test_four_namespaces_and_same_runtime_collision(root);
    test_shared_explicit_directory_collision(root);
    test_alias_singleton_and_concurrent_claimants(root);

    std::error_code ec;
    fs::remove_all(root, ec);
    PULSAR_CHECK(!ec);
    std::fprintf(stdout, "runtime-isolation-probe: PASS cleanup=1\n");
    return EXIT_SUCCESS;
}
