module.exports = {
    uiPort: 1880,
    flowFile: "flows.json",
    httpAdminRoot: false,
    disableEditor: true,
    credentialSecret: false,
    functionExternalModules: false,
    contextStorage: { default: { module: "memory" } },
    logging: { console: { level: "info", metrics: false, audit: false } }
};
