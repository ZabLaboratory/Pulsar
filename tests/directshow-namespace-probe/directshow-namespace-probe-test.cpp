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
                enum directshow_queue_namespace expected)
{
    set_variable("PULSAR_RUNTIME_INSTANCE_ID", runtime_id);
    set_variable("PULSAR_DIRECTSHOW_LEGACY_ALIAS", legacy_alias);
    const enum directshow_queue_namespace actual = directshow_queue_namespace_from_environment();
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
    check_case("stock compatibility", nullptr, nullptr, DIRECTSHOW_QUEUE_NAMESPACE_LEGACY);
    check_case("dedicated id without alias", "runtime-A.1", nullptr,
               DIRECTSHOW_QUEUE_NAMESPACE_DEDICATED);
    check_case("explicit legacy", "runtime-A.1", "true", DIRECTSHOW_QUEUE_NAMESPACE_LEGACY);
    check_case("explicit dedicated", "runtime-A.1", "false", DIRECTSHOW_QUEUE_NAMESPACE_DEDICATED);
    check_case("malformed alias remains dedicated with valid id", "runtime-A.1", "maybe",
               DIRECTSHOW_QUEUE_NAMESPACE_DEDICATED);
    check_case("invalid id overrides legacy", "../escape", "true",
               DIRECTSHOW_QUEUE_NAMESPACE_REJECT);
    check_case("alternate invalid id", "bad/id", "false", DIRECTSHOW_QUEUE_NAMESPACE_REJECT);
    check_case("empty id is explicit and rejected", "", "true", DIRECTSHOW_QUEUE_NAMESPACE_REJECT);
    check_case("alias without id is rejected", nullptr, "true", DIRECTSHOW_QUEUE_NAMESPACE_REJECT);
    check_case("false alias without id is rejected", nullptr, "false", DIRECTSHOW_QUEUE_NAMESPACE_REJECT);
    check_case("empty alias without id is rejected", nullptr, "", DIRECTSHOW_QUEUE_NAMESPACE_REJECT);
    check_case("malformed alias without id", nullptr, "maybe", DIRECTSHOW_QUEUE_NAMESPACE_REJECT);

    set_variable("PULSAR_RUNTIME_INSTANCE_ID", nullptr);
    set_variable("PULSAR_DIRECTSHOW_LEGACY_ALIAS", nullptr);
    return failures == 0 ? 0 : 1;
}
