# Minecraft Fabric 1.21.11 开发环境导出

这是一套从当前 `mina` 模组提取出来的可复用开发环境，目标是让另一个 Minecraft Fabric 项目直接复用相同的基础构建环境。

## 包含内容

- Gradle Wrapper `9.2.0`
- Java Toolchain `21`
- Fabric Loom `1.14.10`
- Minecraft `1.21.11`
- Yarn Mappings `1.21.11+build.1`
- Fabric Loader `0.18.4`
- Fabric API `0.140.2+1.21.11`
- 阿里云 Maven 镜像与 Fabric 官方仓库配置

## 目录说明

- `build.gradle`：通用 Fabric 模组构建脚本
- `settings.gradle`：Gradle 插件仓库与项目名
- `gradle.properties`：版本与项目基础属性
- `gradlew` / `gradlew.bat`：Gradle Wrapper 启动脚本
- `gradle/wrapper/`：Gradle Wrapper 元数据
- `template/`：新项目最小模板文件

## 使用方式

1. 将本目录全部内容复制到目标项目根目录。
2. 修改 `gradle.properties` 中这几个字段：
   - `mod_version`
   - `maven_group`
   - `archives_base_name`
3. 修改 `settings.gradle` 中的 `rootProject.name`。
4. 从 `template/` 复制最小模板文件到目标项目：
   - `template/src/main/resources/fabric.mod.json`
   - `template/src/main/java/mina/MinaMod.java`
5. 按需将包名 `mina`、类名 `MinaMod` 改成你自己的命名。
6. 在项目根目录执行：

```bash
./gradlew build
```

## 最低环境要求

- 已安装 Java 21
- 允许 Gradle 下载 Minecraft / Fabric 依赖

## 迁移建议

- 如果目标项目已经有自己的源码结构，只需复制这套 Gradle 环境文件，不必复制 `template/`。
- 如果目标项目也要使用和 `mina` 相同的国内镜像，可直接保留当前仓库配置。
- 如果目标项目发布到 Maven 仓库，再补充 `publishing` 配置即可。

## 已知边界

- 这只是“开发环境模板”，不包含 `mina` 的业务代码、技能、知识库、运行数据和测试用例。
- 如果目标项目需要运行服务端调试，可直接使用 `./gradlew runServer`，运行目录默认是 `run/`。
