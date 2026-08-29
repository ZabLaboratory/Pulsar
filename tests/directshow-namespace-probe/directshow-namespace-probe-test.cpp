// #248 DirectShow namespace regression gate.  This compiles the exact shared
// header used by the C producer and C++ DirectShow consumer: no test mirror.

#include <windows.h>

#include <cstdio>
#include <cstdlib>

#include "directshow-namespace.h"

namespace {

int failures = 0;

void set_variable(const char *name, const char *value)
{
    if (!SetEnvironmentVariableA(name, value)) {
        std::fprintf(stderr, "SetEnvironmentVariableA(%s) failed: %lu\n", name,
                     static_cast<unsigned long>(GetLastError()));
        std::abort();
    }
}

void check_case(const char *label, const char *runtime_id, const char *legacy_alias,
                enum directshow_consumer_filter_kind filter_kind,
                enum directshow_queue_namespace expected)
{
    set_variable("PULSAR_RUNTIME_INSTANCE_ID", runtime_id);
    set_variable("PULSAR_DIRECTSHOW_LEGACY_ALIAS", legacy_alias);
    const enum directshow_queue_namespace actual = directshow_queue_namespace_for_consumer(filter_kind);
    if (actual != expected) {
        std::fprintf(stderr, "%s: expected %s, got %s\n", label,
                     directshow_queue_namespace_name(expected),
                     directshow_queue_namespace_name(actual));
        ++failures;
    }
}

} // namespace

int main()
{
    check_case("stock unset compatibility", nullptr, nullptr, DIRECTSHOW_CONSUMER_FILTER_STOCK,
               DIRECTSHOW_QUEUE_NAMESPACE_LEGACY);
    check_case("program unset rejects", nullptr, nullptr, DIRECTSHOW_CONSUMER_FILTER_PROGRAM_RETURN,
               DIRECTSHOW_QUEUE_NAMESPACE_REJECT);
    check_case("preview unset rejects", nullptr, nullptr, DIRECTSHOW_CONSUMER_FILTER_PREVIEW_RETURN,
               DIRECTSHOW_QUEUE_NAMESPACE_REJECT);
    check_case("dedicated stock id without alias", "runtime-A.1", nullptr, DIRECTSHOW_CONSUMER_FILTER_STOCK,
               DIRECTSHOW_QUEUE_NAMESPACE_DEDICATED);
    check_case("program id without alias stays dedicated", "runtime-A.1", nullptr,
               DIRECTSHOW_CONSUMER_FILTER_PROGRAM_RETURN, DIRECTSHOW_QUEUE_NAMESPACE_DEDICATED);
    check_case("preview id without alias stays dedicated", "runtime-A.1", nullptr,
               DIRECTSHOW_CONSUMER_FILTER_PREVIEW_RETURN, DIRECTSHOW_QUEUE_NAMESPACE_DEDICATED);
    check_case("explicit stock legacy", "runtime-A.1", "true", DIRECTSHOW_CONSUMER_FILTER_STOCK,
               DIRECTSHOW_QUEUE_NAMESPACE_LEGACY);
    check_case("explicit program legacy", "runtime-A.1", "true", DIRECTSHOW_CONSUMER_FILTER_PROGRAM_RETURN,
               DIRECTSHOW_QUEUE_NAMESPACE_LEGACY);
    check_case("explicit preview legacy", "runtime-A.1", "true", DIRECTSHOW_CONSUMER_FILTER_PREVIEW_RETURN,
               DIRECTSHOW_QUEUE_NAMESPACE_LEGACY);
    check_case("explicit dedicated", "runtime-A.1", "false", DIRECTSHOW_CONSUMER_FILTER_STOCK,
               DIRECTSHOW_QUEUE_NAMESPACE_DEDICATED);
    check_case("malformed alias remains dedicated with valid id", "runtime-A.1", "maybe",
               DIRECTSHOW_CONSUMER_FILTER_STOCK,
               DIRECTSHOW_QUEUE_NAMESPACE_DEDICATED);
    check_case("invalid program id overrides legacy", "../escape", "true",
               DIRECTSHOW_CONSUMER_FILTER_PROGRAM_RETURN,
               DIRECTSHOW_QUEUE_NAMESPACE_REJECT);
    check_case("alternate invalid preview id", "bad/id", "false", DIRECTSHOW_CONSUMER_FILTER_PREVIEW_RETURN,
               DIRECTSHOW_QUEUE_NAMESPACE_REJECT);
    check_case("empty id is explicit and rejected", "", "true", DIRECTSHOW_CONSUMER_FILTER_STOCK,
               DIRECTSHOW_QUEUE_NAMESPACE_REJECT);
    check_case("alias without id is rejected", nullptr, "true", DIRECTSHOW_CONSUMER_FILTER_STOCK,
               DIRECTSHOW_QUEUE_NAMESPACE_REJECT);
    check_case("false alias without id is rejected", nullptr, "false", DIRECTSHOW_CONSUMER_FILTER_STOCK,
               DIRECTSHOW_QUEUE_NAMESPACE_REJECT);
    check_case("empty alias without id is rejected", nullptr, "", DIRECTSHOW_CONSUMER_FILTER_STOCK,
               DIRECTSHOW_QUEUE_NAMESPACE_REJECT);
    check_case("malformed alias without id", nullptr, "maybe", DIRECTSHOW_CONSUMER_FILTER_STOCK,
               DIRECTSHOW_QUEUE_NAMESPACE_REJECT);

    set_variable("PULSAR_RUNTIME_INSTANCE_ID", nullptr);
    set_variable("PULSAR_DIRECTSHOW_LEGACY_ALIAS", nullptr);
    if (directshow_queue_namespace_from_environment() != DIRECTSHOW_QUEUE_NAMESPACE_LEGACY) {
        std::fprintf(stderr, "producer stock compatibility unexpectedly changed\n");
        ++failures;
    }

    set_variable("PULSAR_RUNTIME_INSTANCE_ID", "runtime-A.1");
    set_variable("PULSAR_DIRECTSHOW_LEGACY_ALIAS", "true");
    if (directshow_queue_namespace_from_environment() != DIRECTSHOW_QUEUE_NAMESPACE_LEGACY) {
        std::fprintf(stderr, "producer explicit legacy unexpectedly changed\n");
        ++failures;
    }

    set_variable("PULSAR_RUNTIME_INSTANCE_ID", nullptr);
    set_variable("PULSAR_DIRECTSHOW_LEGACY_ALIAS", nullptr);
    return failures == 0 ? 0 : 1;
}
